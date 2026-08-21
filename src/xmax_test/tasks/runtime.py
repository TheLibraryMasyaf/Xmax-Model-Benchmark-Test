"""Concrete one-task runtime: generate -> preprocess -> evaluate -> sync."""

from __future__ import annotations

from typing import Any

from ..errors import ExternalServiceError
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

        if getattr(self._composition, "_offline_preflight_ok", False):
            return
        for task in tasks:
            if task.get("status") in {"completed", "cancelled"}:
                continue
            if task.get("result_refs", {}).get("run_id"):
                continue
            case = task.get("payload", {}).get("case", {})
            if case.get("generation_mode", "offline") != "offline":
                continue
            adapter = self._composition.offline_adapter(
                run_batch_id=f"runs-{task['task_id'][5:17]}",
                model_id=case.get("model_id")
                or self._composition.project.get("default_model", "x2.0"),
            )
            adapter.preflight()
            self._composition._offline_preflight_ok = True
            return

    def __call__(self, task: dict[str, Any]) -> dict[str, Any]:
        task_id = task["task_id"]
        case = task["payload"]["case"]
        run_batch_id = f"runs-{task_id[5:17]}"
        refs = dict(task.get("result_refs", {}))

        run = self._existing_run(refs, run_batch_id)
        if run is None:
            self.preflight([task])
            self._status(task_id, "generating")
            if case.get("generation_mode") == "realtime":
                controller = self._composition.realtime_controller(
                    run_batch_id=run_batch_id,
                    model_id=case.get("model_id") or self._composition.project.get("default_model", "x2.0"),
                    headed=self._headed,
                )
                run = controller.run_case(
                    case,
                    self._composition.realtime_case_config(case, headed=self._headed),
                )
            else:
                adapter = self._composition.offline_adapter(
                    run_batch_id=run_batch_id,
                    model_id=case.get("model_id") or self._composition.project.get("default_model", "x2.0"),
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
            evaluation_batch_id = "eval-task-" + content_hash(
                {
                    "task_id": task_id,
                    "benchmark_version": self._composition.benchmark.get("benchmark_version"),
                    "scenario_pack_version": self._composition.scenario_pack.get("version"),
                    "judge_registry": self._judge_registry_fingerprint(),
                }
            )[:12]
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

        if self._sync_policy != "none":
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
                    raise ExternalServiceError(
                        f"task {task_id} Feishu reconcile failed: {outcome}"
                    )
                refs = self._save_refs(task_id, reconcile=outcome)

        return {
            **refs,
            "outcome": (
                "completed" if run.get("status") == "completed" else "generation_error"
            ),
        }

    def _judge_registry_fingerprint(self) -> str | None:
        path = self._composition.root / "config" / "judges.json"
        if not path.is_file():
            return None
        return content_hash({"content": path.read_text(encoding="utf-8")})

    def _existing_run(
        self, refs: dict[str, Any], run_batch_id: str
    ) -> dict[str, Any] | None:
        if refs.get("run_id"):
            return self._repository.get_run(refs["run_id"])
        runs = self._repository.list_runs(run_batch_id=run_batch_id)
        return runs[-1] if runs else None

    def _status(self, task_id: str, status: str) -> None:
        self._repository.update_test_task(
            task_id, status, lease_owner=self._lease_owner
        )

    def _save_refs(self, task_id: str, **refs: Any) -> dict[str, Any]:
        task = self._repository.get_test_task(task_id)
        updated = self._repository.update_test_task(
            task_id,
            task["status"],
            lease_owner=self._lease_owner,
            result_refs=refs,
        )
        return updated["result_refs"]
