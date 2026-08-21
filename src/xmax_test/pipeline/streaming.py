"""Bounded per-run handoff for generate -> preprocess -> evaluate -> sync."""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable


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
        generated_queue: queue.Queue[Any] = queue.Queue(maxsize=self._queue_size)
        preprocessed_queue: queue.Queue[Any] = queue.Queue(maxsize=self._queue_size)
        evaluated_queue: queue.Queue[Any] = queue.Queue(maxsize=self._queue_size)
        sentinel = object()
        timings: dict[str, dict[str, float]] = {
            stage: {} for stage in ("generate", "preprocess", "evaluate", "sync")
        }

        def mark(stage: str, key: str) -> None:
            with lock:
                timings[stage].setdefault(key, time.monotonic())

        def add_error(stage: str, entity_id: str | None, exc: Any) -> None:
            with lock:
                outcome.errors[stage].append(
                    {
                        "code": "xmax.streaming_stage_error",
                        "message": str(exc),
                        "stage": stage,
                        "retryable": False,
                        "entity_id": entity_id,
                    }
                )

        def preprocess_worker() -> None:
            while True:
                run = generated_queue.get()
                try:
                    if run is sentinel:
                        if self._evaluate is not None:
                            preprocessed_queue.put(sentinel)
                        return
                    mark("preprocess", "first_item_started")
                    try:
                        result = self._preprocess(run)  # type: ignore[misc]
                    except Exception as exc:
                        add_error("preprocess", run.get("run_id"), exc)
                        continue
                    with lock:
                        outcome.preprocess.append(result)
                    if self._evaluate is not None:
                        preprocessed_queue.put((run, result))
                finally:
                    generated_queue.task_done()

        def evaluate_worker() -> None:
            while True:
                item = preprocessed_queue.get()
                try:
                    if item is sentinel:
                        if self._sync is not None:
                            evaluated_queue.put(sentinel)
                        return
                    run, preprocess = item
                    mark("evaluate", "first_item_started")
                    try:
                        result = self._evaluate(run, preprocess)  # type: ignore[misc]
                    except Exception as exc:
                        add_error("evaluate", run.get("run_id"), exc)
                        continue
                    with lock:
                        outcome.evaluations.append(result)
                    if self._sync is not None:
                        evaluated_queue.put((run, result))
                finally:
                    preprocessed_queue.task_done()

        def sync_worker() -> None:
            while True:
                item = evaluated_queue.get()
                try:
                    if item is sentinel:
                        return
                    run, evaluation = item
                    mark("sync", "first_item_started")
                    try:
                        result = self._sync(run, evaluation)  # type: ignore[misc]
                    except Exception as exc:
                        add_error("sync", run.get("run_id"), exc)
                        continue
                    with lock:
                        outcome.sync.append(result)
                finally:
                    evaluated_queue.task_done()

        workers: list[threading.Thread] = []
        if self._preprocess is not None:
            workers.append(
                threading.Thread(
                    target=preprocess_worker,
                    name="xmax-preprocess-worker",
                    daemon=True,
                )
            )
        if self._evaluate is not None:
            workers.append(
                threading.Thread(
                    target=evaluate_worker,
                    name="xmax-evaluate-worker",
                    daemon=True,
                )
            )
        if self._sync is not None:
            workers.append(
                threading.Thread(
                    target=sync_worker,
                    name="xmax-sync-worker",
                    daemon=True,
                )
            )
        for worker in workers:
            worker.start()

        consecutive_failure: tuple[str, int] | None = None
        aborted = False
        abort_reason: str | None = None
        for case in cases:
            mark("generate", "first_item_started")
            try:
                run = self._generate(case)
                if self._on_generated is not None:
                    self._on_generated(run)
                with lock:
                    outcome.runs.append(run)
                if run.get("status") != "completed":
                    add_error(
                        "generate",
                        run.get("run_id"),
                        f"generation finished with status {run.get('status')}",
                    )
                    continue
                consecutive_failure = None
                if self._preprocess is not None:
                    generated_queue.put(run)
            except Exception as exc:
                add_error("generate", case.get("case_id"), exc)
                fingerprint = f"{type(exc).__name__}:{exc}"
                count = (
                    consecutive_failure[1] + 1
                    if consecutive_failure and consecutive_failure[0] == fingerprint
                    else 1
                )
                consecutive_failure = (fingerprint, count)
                retryable = getattr(exc, "retryable", True)
                if not retryable or count >= self._circuit_breaker_threshold:
                    aborted = True
                    abort_reason = (
                        f"non-retryable generation failure: {exc}"
                        if not retryable
                        else f"same generation failure repeated {count} times: {exc}"
                    )
                    break
        mark("generate", "all_items_submitted")

        if self._preprocess is not None:
            generated_queue.put(sentinel)
            generated_queue.join()
        if self._evaluate is not None:
            preprocessed_queue.join()
        if self._sync is not None:
            evaluated_queue.join()
        for worker in workers:
            worker.join()

        generation_finished = timings["generate"].get("all_items_submitted")
        preprocess_started = timings["preprocess"].get("first_item_started")
        evaluation_started = timings["evaluate"].get("first_item_started")
        sync_started = timings["sync"].get("first_item_started")
        outcome.metadata = {
            "execution_mode": "streaming",
            "queue_size": self._queue_size,
            "counts": {
                "generated": len(outcome.runs),
                "generation_reused": sum(
                    1 for run in outcome.runs if run.get("stream_reused")
                ),
                "preprocessed": len(outcome.preprocess),
                "evaluated": len(outcome.evaluations),
                "synced": len(outcome.sync),
            },
            "overlap_observed": {
                "preprocess_before_generation_finished": bool(
                    len(outcome.runs) > 1
                    and preprocess_started
                    and generation_finished
                    and preprocess_started < generation_finished
                ),
                "evaluate_before_generation_finished": bool(
                    len(outcome.runs) > 1
                    and evaluation_started
                    and generation_finished
                    and evaluation_started < generation_finished
                ),
                "sync_before_generation_finished": bool(
                    len(outcome.runs) > 1
                    and sync_started
                    and generation_finished
                    and sync_started < generation_finished
                ),
            },
            "circuit_breaker": {
                "threshold": self._circuit_breaker_threshold,
                "aborted": aborted,
                "reason": abort_reason,
            },
        }
        return outcome
