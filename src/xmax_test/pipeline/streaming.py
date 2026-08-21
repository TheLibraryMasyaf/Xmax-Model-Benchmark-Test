"""Bounded per-run handoff for generate -> preprocess -> evaluate -> sync."""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from ..errors import EvaluationBudgetPausedError, RealtimeUnavailableError

StageCallable = Callable[..., dict[str, Any]]


@dataclass
class StreamingOutcome:
    runs: list[dict[str, Any]] = field(default_factory=list)
    preprocess: list[dict[str, Any]] = field(default_factory=list)
    evaluations: list[dict[str, Any]] = field(default_factory=list)
    sync: list[dict[str, Any]] = field(default_factory=list)
    errors: dict[str, list[dict[str, Any]]] = field(
        default_factory=lambda: {
            "generate": [],
            "preprocess": [],
            "evaluate": [],
            "sync": [],
        }
    )
    metadata: dict[str, Any] = field(default_factory=dict)


class StreamingPipelineCoordinator:
    """Schedule independent stage services with bounded per-run queues."""

    def __init__(
        self,
        *,
        generate_case: StageCallable,
        preprocess_run: StageCallable | None = None,
        evaluate_run: StageCallable | None = None,
        sync_run: StageCallable | None = None,
        on_generated: Callable[[dict[str, Any]], None] | None = None,
        queue_size: int = 4,
        circuit_breaker_threshold: int = 3,
    ) -> None:
        if queue_size < 1:
            raise ValueError("streaming queue_size must be at least 1")
        if evaluate_run is not None and preprocess_run is None:
            raise ValueError("streaming evaluation requires preprocessing")
        if sync_run is not None and evaluate_run is None:
            raise ValueError("streaming sync requires evaluation")
        if circuit_breaker_threshold < 1:
            raise ValueError("circuit_breaker_threshold must be at least 1")
        self._generate = generate_case
        self._preprocess = preprocess_run
        self._evaluate = evaluate_run
        self._sync = sync_run
        self._on_generated = on_generated
        self._queue_size = queue_size
        self._circuit_breaker_threshold = circuit_breaker_threshold

    def run(self, cases: Iterable[dict[str, Any]]) -> StreamingOutcome:
        outcome = StreamingOutcome()
        lock = threading.Lock()
        queues = [queue.Queue(maxsize=self._queue_size) for _ in range(3)]
        sentinel = object()
        timings: dict[str, dict[str, float]] = {
            stage: {} for stage in ("generate", "preprocess", "evaluate", "sync")
        }
        evaluation_paused = threading.Event()
        deferred_evaluation_run_ids: list[str] = []
        workers = self._workers(
            outcome,
            lock,
            queues,
            sentinel,
            timings,
            evaluation_paused,
            deferred_evaluation_run_ids,
        )
        for worker in workers:
            worker.start()
        aborted, abort_reason = self._generate_all(cases, outcome, lock, queues[0], timings)
        self._mark(lock, timings, "generate", "all_items_submitted")
        self._drain(queues, sentinel)
        for worker in workers:
            worker.join()
        outcome.metadata = self._metadata(outcome, timings, aborted, abort_reason)
        outcome.metadata["evaluation_gate"] = {
            "paused": evaluation_paused.is_set(),
            "deferred_count": len(deferred_evaluation_run_ids),
            "deferred_run_ids": deferred_evaluation_run_ids,
        }
        return outcome

    def _workers(
        self,
        outcome: StreamingOutcome,
        lock: threading.Lock,
        queues: list[queue.Queue[Any]],
        sentinel: object,
        timings: dict[str, dict[str, float]],
        evaluation_paused: threading.Event,
        deferred_evaluation_run_ids: list[str],
    ) -> list[threading.Thread]:
        specs = [
            (
                "preprocess",
                self._preprocess,
                queues[0],
                queues[1] if self._evaluate is not None else None,
                outcome.preprocess,
            ),
            (
                "evaluate",
                self._evaluate,
                queues[1],
                queues[2] if self._sync is not None else None,
                outcome.evaluations,
            ),
            ("sync", self._sync, queues[2], None, outcome.sync),
        ]
        workers = []
        for stage, function, input_queue, output_queue, result_list in specs:
            if function is None:
                continue
            worker = threading.Thread(
                target=self._stage_worker,
                args=(
                    stage,
                    function,
                    input_queue,
                    output_queue,
                    result_list,
                    outcome,
                    lock,
                    sentinel,
                    timings,
                    evaluation_paused,
                    deferred_evaluation_run_ids,
                ),
                name=f"xmax-{stage}-worker",
                daemon=True,
            )
            workers.append(worker)
        return workers

    def _stage_worker(
        self,
        stage: str,
        function: StageCallable,
        input_queue: queue.Queue[Any],
        output_queue: queue.Queue[Any] | None,
        result_list: list[dict[str, Any]],
        outcome: StreamingOutcome,
        lock: threading.Lock,
        sentinel: object,
        timings: dict[str, dict[str, float]],
        evaluation_paused: threading.Event,
        deferred_evaluation_run_ids: list[str],
    ) -> None:
        while True:
            item = input_queue.get()
            try:
                if item is sentinel:
                    if output_queue is not None:
                        output_queue.put(sentinel)
                    return
                run = item[0] if isinstance(item, tuple) else item
                arguments = item if isinstance(item, tuple) else (item,)
                if stage == "evaluate" and evaluation_paused.is_set():
                    with lock:
                        deferred_evaluation_run_ids.append(run.get("run_id"))
                    continue
                self._mark(lock, timings, stage, "first_item_started")
                try:
                    result = function(*arguments)
                except EvaluationBudgetPausedError as exc:
                    if stage != "evaluate":
                        self._add_error(outcome, lock, stage, run.get("run_id"), exc)
                        continue
                    first_pause = not evaluation_paused.is_set()
                    evaluation_paused.set()
                    with lock:
                        deferred_evaluation_run_ids.append(run.get("run_id"))
                    if first_pause:
                        self._add_error(outcome, lock, stage, run.get("run_id"), exc)
                    continue
                except Exception as exc:
                    self._add_error(outcome, lock, stage, run.get("run_id"), exc)
                    continue
                with lock:
                    result_list.append(result)
                if output_queue is not None:
                    output_queue.put((run, result))
            finally:
                input_queue.task_done()

    def _generate_all(
        self,
        cases: Iterable[dict[str, Any]],
        outcome: StreamingOutcome,
        lock: threading.Lock,
        generated_queue: queue.Queue[Any],
        timings: dict[str, dict[str, float]],
    ) -> tuple[bool, str | None]:
        consecutive: tuple[str, int] | None = None
        for case in cases:
            self._mark(lock, timings, "generate", "first_item_started")
            try:
                run = self._generate(case)
                if self._on_generated is not None:
                    self._on_generated(run)
                with lock:
                    outcome.runs.append(run)
                if run.get("status") != "completed":
                    self._add_error(
                        outcome,
                        lock,
                        "generate",
                        run.get("run_id"),
                        f"generation finished with status {run.get('status')}",
                    )
                    continue
                consecutive = None
                if self._preprocess is not None:
                    generated_queue.put(run)
            except RealtimeUnavailableError as exc:
                # A realtime case the harness refuses (e.g. unsupported media
                # MIME type) is a known condition, not a transient failure.
                # Record it but do not let it trip the circuit breaker: the
                # remaining offline cases are still valid and must drain.
                self._add_error(outcome, lock, "generate", case.get("case_id"), exc)
                consecutive = None
                continue
            except Exception as exc:
                self._add_error(outcome, lock, "generate", case.get("case_id"), exc)
                consecutive = self._failure_count(consecutive, exc)
                retryable = getattr(exc, "retryable", True)
                if not retryable:
                    return True, f"non-retryable generation failure: {exc}"
                if consecutive[1] >= self._circuit_breaker_threshold:
                    return (
                        True,
                        f"same generation failure repeated {consecutive[1]} times: {exc}",
                    )
        return False, None

    @staticmethod
    def _failure_count(previous: tuple[str, int] | None, exc: Exception) -> tuple[str, int]:
        fingerprint = f"{type(exc).__name__}:{exc}"
        count = previous[1] + 1 if previous and previous[0] == fingerprint else 1
        return fingerprint, count

    def _drain(self, queues: list[queue.Queue[Any]], sentinel: object) -> None:
        if self._preprocess is not None:
            queues[0].put(sentinel)
            queues[0].join()
        if self._evaluate is not None:
            queues[1].join()
        if self._sync is not None:
            queues[2].join()

    @staticmethod
    def _mark(
        lock: threading.Lock, timings: dict[str, dict[str, float]], stage: str, key: str
    ) -> None:
        with lock:
            timings[stage].setdefault(key, time.monotonic())

    @staticmethod
    def _add_error(
        outcome: StreamingOutcome,
        lock: threading.Lock,
        stage: str,
        entity_id: str | None,
        exc: Any,
    ) -> None:
        with lock:
            outcome.errors[stage].append(
                {
                    "code": getattr(exc, "code", "xmax.streaming_stage_error"),
                    "message": str(exc),
                    "stage": stage,
                    "retryable": bool(getattr(exc, "retryable", False)),
                    "entity_id": entity_id,
                }
            )

    def _metadata(
        self,
        outcome: StreamingOutcome,
        timings: dict[str, dict[str, float]],
        aborted: bool,
        abort_reason: str | None,
    ) -> dict[str, Any]:
        generation_finished = timings["generate"].get("all_items_submitted")
        started = {
            stage: timings[stage].get("first_item_started")
            for stage in ("preprocess", "evaluate", "sync")
        }
        return {
            "execution_mode": "streaming",
            "queue_size": self._queue_size,
            "counts": {
                "generated": len(outcome.runs),
                "generation_reused": sum(1 for run in outcome.runs if run.get("stream_reused")),
                "preprocessed": len(outcome.preprocess),
                "evaluated": len(outcome.evaluations),
                "synced": len(outcome.sync),
            },
            "overlap_observed": {
                "preprocess_before_generation_finished": bool(
                    len(outcome.runs) > 1
                    and started["preprocess"]
                    and generation_finished
                    and started["preprocess"] < generation_finished
                ),
                "evaluate_before_generation_finished": bool(
                    len(outcome.runs) > 1
                    and started["evaluate"]
                    and generation_finished
                    and started["evaluate"] < generation_finished
                ),
                "sync_before_generation_finished": bool(
                    len(outcome.runs) > 1
                    and started["sync"]
                    and generation_finished
                    and started["sync"] < generation_finished
                ),
            },
            "circuit_breaker": {
                "threshold": self._circuit_breaker_threshold,
                "aborted": aborted,
                "reason": abort_reason,
            },
        }
