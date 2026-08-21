"""Persistent task-batch services.

The allocator converts a frozen TestPlan into immutable per-Case tasks.  The
generic worker owns leasing/retry/idempotency; a runtime callback owns the
actual generation/evaluation/sync implementation.
"""

from __future__ import annotations

from typing import Any, Callable

from ..hashing import content_hash
from ..time import utc_now


class TaskAllocator:
    def __init__(self, repository: Any, clock: Any = None) -> None:
        self._repository = repository
        self._clock = clock

    def create(self, plan: dict[str, Any]) -> dict[str, Any]:
        task_batch_id = f"tasks-{plan['plan_hash'][:12]}"
        created_at = self._clock.now() if self._clock else utc_now()
        tasks: list[dict[str, Any]] = []
        for allocation_index, case in enumerate(plan.get("cases", []), start=1):
            task_id = "task-" + content_hash(
                {
                    "plan_hash": plan["plan_hash"],
                    "case_id": case["case_id"],
                    "allocation_index": allocation_index,
                }
            )[:16]
            tasks.append(
                {
                    "task_id": task_id,
                    "task_batch_id": task_batch_id,
                    "plan_id": plan["plan_id"],
                    "case_id": case["case_id"],
                    "status": "pending",
                    "payload": {
                        "schema_version": "1",
                        "plan_hash": plan["plan_hash"],
                        "allocation_index": allocation_index,
                        "case": case,
                    },
                    "result_refs": {},
                    "created_at": created_at,
                }
            )
        self._repository.save_test_tasks(tasks)
        return {
            "task_batch_id": task_batch_id,
            "task_ids": [task["task_id"] for task in tasks],
            "tasks": tasks,
        }


class TaskWorker:
    """Lease and execute frozen tasks without deciding combinations."""

    def __init__(
        self,
        repository: Any,
        execute_task: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> None:
        self._repository = repository
        self._execute_task = execute_task

    def run_task(
        self,
        task_id: str,
        *,
        lease_owner: str,
        lease_seconds: int = 900,
        resume: bool = False,
    ) -> dict[str, Any]:
        current = self._repository.get_test_task(task_id)
        if current["status"] == "completed":
            return {**current, "reused": True}
        self._preflight([current])
        claimed = self._repository.claim_test_task(
            task_id,
            lease_owner,
            lease_seconds=lease_seconds,
            retry_error=resume,
        )
        if claimed is None:
            return self._repository.get_test_task(task_id)
        return self._execute_claimed(claimed, lease_owner)

    def run_batch(
        self,
        task_batch_id: str,
        *,
        lease_owner: str,
        lease_seconds: int = 900,
        max_tasks: int | None = None,
    ) -> dict[str, Any]:
        # Run transport/dependency checks before claiming even one task.  A
        # batch-wide infrastructure failure must leave every task untouched,
        # rather than manufacturing hundreds of per-Case error rows.
        self._preflight(
            self._repository.list_test_tasks(task_batch_id=task_batch_id)
        )
        processed: list[str] = []
        errors: list[dict[str, Any]] = []
        while max_tasks is None or len(processed) < max_tasks:
            task = self._repository.claim_next_test_task(
                task_batch_id,
                lease_owner,
                lease_seconds=lease_seconds,
            )
            if task is None:
                break
            result = self._execute_claimed(task, lease_owner)
            processed.append(task["task_id"])
            if result["status"] == "error":
                errors.append(result.get("last_error") or {"task_id": task["task_id"]})
        return {
            **self._repository.test_task_summary(task_batch_id),
            "processed_task_ids": processed,
            "errors": errors,
        }

    def _preflight(self, tasks: list[dict[str, Any]]) -> None:
        preflight = getattr(self._execute_task, "preflight", None)
        if callable(preflight):
            preflight(tasks)

    def _execute_claimed(
        self, task: dict[str, Any], lease_owner: str
    ) -> dict[str, Any]:
        try:
            result_refs = self._execute_task(task) or {}
            return self._repository.update_test_task(
                task["task_id"],
                "completed",
                lease_owner=lease_owner,
                result_refs=result_refs,
            )
        except Exception as exc:
            error = {
                "code": getattr(exc, "code", "xmax.task_execution_error"),
                "message": str(exc),
                "stage": getattr(exc, "stage", "task_worker"),
                "retryable": bool(getattr(exc, "retryable", True)),
                "task_id": task["task_id"],
            }
            return self._repository.update_test_task(
                task["task_id"],
                "error",
                lease_owner=lease_owner,
                last_error=error,
            )
