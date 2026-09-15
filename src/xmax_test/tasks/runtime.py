"""Concrete one-task runtime: generate -> preprocess -> evaluate -> sync."""

from __future__ import annotations

from typing import Any

from ..errors import ContractError, ExternalServiceError
from ..hashing import content_hash


class PipelineTaskRuntime:
    def __init__(
        self,
        composition: Any,
        *,
        lease_owner: str,
        sync_policy: str = "full",
        reconcile: bool = True,
        headed: bool = False,
    ) -> None:
        self._composition = composition
        self._repository = composition.database
        self._lease_owner = lease_owner
        self._sync_policy = sync_policy
        self._reconcile = reconcile
        self._headed = headed

    def preflight(self, tasks: list[dict[str, Any]]) -> None:
        """Validate shared offline transport before TaskWorker claims a task."""

        checked_providers = getattr(
            self._composition, "_offline_preflight_providers", set()
        )
        for task in tasks:
            if task.get("status") in {"completed", "cancelled"}:
                continue
            if task.get("result_refs", {}).get("run_id"):
                continue
            case = task.get("payload", {}).get("case", {})
            if case.get("generation_mode", "offline") != "offline":
                continue
            provider = case.get("generation_provider", "xmax")
            if provider in checked_providers:
                continue
            adapter = self._composition.offline_adapter(
                run_batch_id=f"runs-{task['task_id'][5:17]}",
                model_id=case.get("model_id")
                or self._composition.project.get("default_model", "x2.0"),
                provider=provider,
            )
            adapter.preflight()
            self._composition._offline_preflight_providers = {
                *checked_providers,
                provider,
            }
            return

    def evaluation_available(self) -> bool:
        gate = getattr(self._composition, "_evaluation_budget_gate", None)
        return gate is None or gate.status().get("status") == "open"

    def __call__(self, task: dict[str, Any]) -> dict[str, Any]:
        task_id = task["task_id"]
        case = task["payload"]["case"]
        run_batch_id = f"runs-{task_id[5:17]}"
        refs = dict(task.get("result_refs", {}))

        run = self._existing_run(refs, run_batch_id, case)
        if run is not None and run.get("run_batch_id") != run_batch_id:
            # Adopt an idempotent standalone generation into the task pipeline.
            # This prevents a paid duplicate when an operator started the
            # frozen plan with ``generate`` before switching to ``worker``.
            self._repository.set_run_batch_id(run["run_id"], run_batch_id)
            run = self._repository.get_run(run["run_id"])
            refs = self._save_refs(
                task_id, run_id=run["run_id"], run_batch_id=run_batch_id
            )
        if run is None:
            self.preflight([task])
            self._status(task_id, "generating")
            if case.get("generation_mode") == "realtime":
                if case.get("generation_provider", "xmax") != "xmax":
                    raise ContractError("realtime generation remains XMAX-only")
                controller = self._composition.realtime_controller(
                    run_batch_id=run_batch_id,
                    model_id=case.get("model_id")
                    or self._composition.project.get("default_model", "x2.0"),
                    headed=self._headed,
                )
                run = controller.run_case(
                    case,
                    self._composition.realtime_case_config(case, headed=self._headed),
                )
            else:
                adapter = self._composition.offline_adapter(
                    run_batch_id=run_batch_id,
                    model_id=case.get("model_id")
                    or self._composition.project.get("default_model", "x2.0"),
                    provider=case.get("generation_provider", "xmax"),
                )
                run = adapter.run_case(case)
            self._repository.set_run_batch_id(run["run_id"], run_batch_id)
            refs = self._save_refs(task_id, run_id=run["run_id"], run_batch_id=run_batch_id)

        evaluation: dict[str, Any] | None = None
        if run.get("status") == "completed":
            self._status(task_id, "preprocessing")
            preprocess = self._composition.preprocess_service().build(run)
            refs = self._save_refs(task_id, preprocess_id=preprocess["preprocess_id"])

            self._status(task_id, "evaluating")
            evaluation_batch_id = (
                "eval-task-"
                + content_hash(
                    {
                        "task_id": task_id,
                        "benchmark_version": self._composition.benchmark.get("benchmark_version"),
                        "scenario_pack_version": self._composition.scenario_pack.get("version"),
                        "judge_registry": self._judge_registry_fingerprint(),
                    }
                )[:12]
            )
            existing = self._repository.list_evaluation_results(
                run_id=run["run_id"], evaluation_batch_id=evaluation_batch_id
            )
            evaluation = (
                existing[-1]
                if existing
                else self._composition.evaluation_orchestrator().evaluate_run(
                    run, evaluation_batch_id, preprocess=preprocess
                )
            )
            refs = self._save_refs(
                task_id,
                evaluation_id=evaluation["evaluation_id"],
                evaluation_batch_id=evaluation_batch_id,
            )

        if self._sync_policy != "none" and run.get("status") == "completed":
            self._status(task_id, "syncing")
            evaluations = {run["run_id"]: evaluation} if evaluation else {}
            item_sync = self._composition.feishu_sync_service().sync_case_run(
                run, evaluation, policy=self._sync_policy, dry_run=False
            )
            sync = {"policy": self._sync_policy, "items": [item_sync], "errors": []}
            refs = self._save_refs(task_id, sync=sync)
            if self._reconcile:
                outcome = self._composition.feishu_reconcile().reconcile(
                    run_ids=[run["run_id"]], evaluations=evaluations
                )
                if not outcome.get("ok"):
                    raise ExternalServiceError(f"task {task_id} Feishu reconcile failed: {outcome}")
                refs = self._save_refs(task_id, reconcile=outcome)

        return {
            **refs,
            "outcome": ("completed" if run.get("status") == "completed" else "generation_error"),
        }

    def _judge_registry_fingerprint(self) -> str | None:
        path = self._composition.root / "config" / "judges.json"
        if not path.is_file():
            return None
        return content_hash({"content": path.read_text(encoding="utf-8")})

    def _existing_run(
        self, refs: dict[str, Any], run_batch_id: str, case: dict[str, Any] | str
    ) -> dict[str, Any] | None:
        case_id = case.get("case_id") if isinstance(case, dict) else case
        if refs.get("run_id"):
            run = self._repository.get_run(refs["run_id"])
            if not case_id or run.get("case_id") != case_id:
                raise ContractError(
                    f"task Run ref {run.get('run_id')} does not belong to Case {case_id}"
                )
            if (
                run.get("run_batch_id") != run_batch_id
                and (
                    not isinstance(case, dict)
                    or run.get("metrics", {}).get("generation_signature")
                    != case.get("generation_signature")
                )
            ):
                raise ContractError(
                    f"task Run ref {run.get('run_id')} does not belong to batch {run_batch_id}"
                )
            return run
        if not case_id:
            return None
        runs = self._repository.list_runs(run_batch_id=run_batch_id, case_id=case_id)
        if runs:
            return max(runs, key=lambda item: (item.get("created_at", ""), item["run_id"]))
        signature = case.get("generation_signature") if isinstance(case, dict) else None
        if not signature:
            return None
        reusable = [
            item
            for item in self._repository.list_runs(case_id=case_id, status="completed")
            if item.get("metrics", {}).get("generation_signature") == signature
        ]
        return (
            max(reusable, key=lambda item: (item.get("created_at", ""), item["run_id"]))
            if reusable
            else None
        )

    def _status(self, task_id: str, status: str) -> None:
        self._repository.update_test_task(task_id, status, lease_owner=self._lease_owner)

    def _save_refs(self, task_id: str, **refs: Any) -> dict[str, Any]:
        task = self._repository.get_test_task(task_id)
        updated = self._repository.update_test_task(
            task_id,
            task["status"],
            lease_owner=self._lease_owner,
            result_refs=refs,
        )
        return updated["result_refs"]
