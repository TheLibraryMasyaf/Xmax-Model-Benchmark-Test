"""SQLite metadata repository.

Database stores business IDs, status, versions, hashes and URIs; never video
blobs. Tables follow ``docs/storage.md``. All list methods return stable
sorted rows and support optional cursors. Write methods accept
``expected_version`` so concurrent overwrites are detected.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from ..errors import ConflictError, ContractError, DuplicateError, NotFoundError, StateError
from ..time import Clock, SystemClock, utc_now
from .migrations import migrate

TERMINAL_STATUSES = {"completed", "error", "cancelled"}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class SqliteMetadataRepository:
    """Explicit business methods over the logical tables in storage.md."""

    def __init__(self, database_path: str | Path, clock: Clock | None = None) -> None:
        self._path = Path(database_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock or SystemClock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout = 30000")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._lock = threading.RLock()
        migrate(self._conn)

    # ------------------------------------------------------------------
    # generic append / event log
    # ------------------------------------------------------------------
    def append_event(
        self,
        run_id: str,
        event: str,
        *,
        timestamp: str | None = None,
        payload: dict[str, Any] | None = None,
        external_key: str | None = None,
    ) -> int:
        timestamp = timestamp or self._clock.now()
        with self._lock:
            if external_key is not None:
                existing = self._conn.execute(
                    "SELECT sequence FROM run_events WHERE run_id = ? AND external_key = ?",
                    (run_id, external_key),
                ).fetchone()
                if existing is not None:
                    return int(existing["sequence"])
            with self._conn:
                self._conn.execute(
                    "INSERT INTO run_events(run_id, sequence, event, timestamp, payload, "
                    "external_key) SELECT ?, COALESCE(MAX(sequence), 0) + 1, ?, ?, ?, ? "
                    "FROM run_events WHERE run_id = ?",
                    (run_id, event, timestamp, _json(payload or {}), external_key, run_id),
                )
                row = self._conn.execute(
                    "SELECT sequence FROM run_events WHERE run_id = ? "
                    "ORDER BY sequence DESC LIMIT 1",
                    (run_id,),
                ).fetchone()
            return int(row["sequence"])

    def get_event_log(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT run_id, sequence, event, timestamp, payload, external_key "
            "FROM run_events WHERE run_id = ? ORDER BY sequence ASC",
            (run_id,),
        ).fetchall()
        result = []
        for row in rows:
            item = self._row_dict(row)
            item["payload"] = json.loads(item["payload"])
            result.append(item)
        return result

    def rebuild_run_from_events(self, run_id: str) -> dict[str, Any] | None:
        """Replay events to rebuild current run state (documented test)."""

        events = self.get_event_log(run_id)
        if not events:
            return None
        state: dict[str, Any] = {"run_id": run_id, "events": events, "status": "planned"}
        for item in events:
            payload = item.get("payload", {})
            if item["event"] in {
                "run_created",
                "status_planned",
                "status_running",
                "status_completed",
                "status_error",
                "status_cancelled",
            }:
                state["status"] = item["event"].replace("status_", "").replace("run_created", "planned")
            for key, value in payload.items():
                state[key] = value
        return state

    # ------------------------------------------------------------------
    # assets
    # ------------------------------------------------------------------
    def upsert_asset(self, asset: dict[str, Any]) -> dict[str, Any]:
        asset_id = asset.get("asset_id")
        sha256 = asset.get("sha256")
        if not asset_id or not sha256:
            raise ContractError("asset requires asset_id and sha256")
        existing = self.find_asset_by_sha256(sha256)
        if existing is not None and existing["asset_id"] != asset_id:
            raise DuplicateError(
                f"sha256 {sha256} already registered as {existing['asset_id']}"
            )
        with self._conn:
            self._conn.execute(
                "INSERT INTO assets(asset_id, kind, uri, sha256, bytes, mime_type, "
                "source, status, media, metadata, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(asset_id) DO UPDATE SET status=excluded.status, "
                "media=excluded.media, metadata=excluded.metadata",
                (
                    asset_id,
                    asset.get("kind", ""),
                    asset.get("uri", ""),
                    sha256,
                    int(asset.get("bytes", 0)),
                    asset.get("mime_type"),
                    _json(asset.get("source", {})),
                    asset.get("status", "discovered"),
                    _json(asset.get("media", {})),
                    _json(asset.get("metadata", {})),
                    asset.get("created_at", utc_now()),
                ),
            )
        return self.get_asset(asset_id)

    def get_asset(self, asset_id: str) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT * FROM assets WHERE asset_id = ?", (asset_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"asset not found: {asset_id}", entity_id=asset_id)
        return self._asset_dict(row)

    def find_asset_by_sha256(self, sha256: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM assets WHERE sha256 = ?", (sha256,)
        ).fetchone()
        return self._asset_dict(row) if row is not None else None

    def update_asset_status(self, asset_id: str, status: str) -> None:
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE assets SET status = ? WHERE asset_id = ?", (status, asset_id)
            )
            if cursor.rowcount == 0:
                raise NotFoundError(f"asset not found: {asset_id}", entity_id=asset_id)

    def list_assets(
        self,
        *,
        status: str | None = None,
        kind: str | None = None,
        cursor: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if cursor:
            clauses.append("asset_id > ?")
            params.append(cursor)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        rows = self._conn.execute(
            f"SELECT * FROM assets{where} ORDER BY asset_id ASC LIMIT ?", params
        ).fetchall()
        return [self._asset_dict(row) for row in rows]

    # ------------------------------------------------------------------
    # test plans and cases
    # ------------------------------------------------------------------
    def save_test_plan(self, plan: dict[str, Any]) -> None:
        plan_hash = plan.get("plan_hash")
        if not plan_hash:
            raise ContractError("test plan requires plan_hash")
        existing = self._conn.execute(
            "SELECT plan_hash FROM test_plans WHERE plan_id = ? AND plan_version = ?",
            (plan.get("plan_id"), plan.get("plan_version")),
        ).fetchone()
        if existing is not None and existing["plan_hash"] != plan_hash:
            raise DuplicateError(
                f"plan {plan.get('plan_id')}@{plan.get('plan_version')} "
                "already exists with a different hash"
            )
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO test_plans(plan_id, plan_version, plan_hash, "
                "payload, frozen, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    plan.get("plan_id"),
                    plan.get("plan_version"),
                    plan_hash,
                    _json(plan),
                    1 if plan.get("frozen") else 0,
                    plan.get("created_at", utc_now()),
                ),
            )
        for case in plan.get("cases", []):
            self.upsert_test_case(case, plan.get("plan_id", ""))

    def get_test_plan(self, plan_id: str, plan_version: str | None = None) -> dict[str, Any]:
        if plan_version:
            row = self._conn.execute(
                "SELECT payload FROM test_plans WHERE plan_id = ? AND plan_version = ?",
                (plan_id, plan_version),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT payload FROM test_plans WHERE plan_id = ? "
                "ORDER BY plan_version DESC LIMIT 1",
                (plan_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"test plan not found: {plan_id}", entity_id=plan_id)
        return json.loads(row["payload"])

    def find_plan_by_hash(self, plan_hash: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT payload FROM test_plans WHERE plan_hash = ?", (plan_hash,)
        ).fetchone()
        return json.loads(row["payload"]) if row is not None else None

    def upsert_test_case(self, case: dict[str, Any], plan_id: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO test_cases(case_id, plan_id, payload, created_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(case_id) DO UPDATE SET payload=excluded.payload",
                (case.get("case_id"), plan_id, _json(case), utc_now()),
            )

    def get_test_case(self, case_id: str) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT payload FROM test_cases WHERE case_id = ?", (case_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"test case not found: {case_id}", entity_id=case_id)
        return json.loads(row["payload"])

    def list_test_cases(self, plan_id: str | None = None) -> list[dict[str, Any]]:
        if plan_id:
            rows = self._conn.execute(
                "SELECT payload FROM test_cases WHERE plan_id = ? ORDER BY case_id",
                (plan_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT payload FROM test_cases ORDER BY case_id"
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    # ------------------------------------------------------------------
    # frozen test-task queue
    # ------------------------------------------------------------------
    def save_test_tasks(self, tasks: list[dict[str, Any]]) -> None:
        """Idempotently persist the exact Cases selected by a Test Plan."""

        now = self._clock.now()
        with self._lock:
            with self._conn:
                for task in tasks:
                    existing = self._conn.execute(
                        "SELECT payload FROM test_tasks WHERE task_id = ?",
                        (task.get("task_id"),),
                    ).fetchone()
                    payload = _json(task.get("payload", {}))
                    if existing is not None and existing["payload"] != payload:
                        raise DuplicateError(
                            f"test task {task.get('task_id')} already exists with a different payload"
                        )
                    self._conn.execute(
                        "INSERT OR IGNORE INTO test_tasks("
                        "task_id, task_batch_id, plan_id, case_id, status, payload, "
                        "lease_owner, lease_expires_at, attempt_count, result_refs, "
                        "last_error, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, 'pending', ?, NULL, NULL, 0, '{}', NULL, ?, ?)",
                        (
                            task.get("task_id"),
                            task.get("task_batch_id"),
                            task.get("plan_id"),
                            task.get("case_id"),
                            payload,
                            task.get("created_at", now),
                            task.get("created_at", now),
                        ),
                    )

    def get_test_task(self, task_id: str) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT * FROM test_tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"test task not found: {task_id}", entity_id=task_id)
        return self._task_dict(row)

    def list_test_tasks(
        self,
        *,
        task_batch_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if task_batch_id:
            clauses.append("task_batch_id = ?")
            params.append(task_batch_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self._conn.execute(
            f"SELECT * FROM test_tasks{where} ORDER BY task_id", params
        ).fetchall()
        return [self._task_dict(row) for row in rows]

    def claim_next_test_task(
        self,
        task_batch_id: str,
        lease_owner: str,
        *,
        lease_seconds: int = 900,
    ) -> dict[str, Any] | None:
        return self._claim_test_task(
            task_batch_id=task_batch_id,
            task_id=None,
            lease_owner=lease_owner,
            lease_seconds=lease_seconds,
        )

    def claim_test_task(
        self,
        task_id: str,
        lease_owner: str,
        *,
        lease_seconds: int = 900,
        retry_error: bool = False,
    ) -> dict[str, Any] | None:
        current = self.get_test_task(task_id)
        active = {"leased", "generating", "preprocessing", "evaluating", "syncing"}
        if (
            current["status"] in active
            and current.get("lease_owner") == lease_owner
            and (current.get("lease_expires_at") or "") > self._clock.now()
        ):
            return current
        if retry_error:
            with self._conn:
                self._conn.execute(
                    "UPDATE test_tasks SET status='pending', last_error=NULL, updated_at=? "
                    "WHERE task_id=? AND status='error'",
                    (self._clock.now(), task_id),
                )
        return self._claim_test_task(
            task_batch_id=None,
            task_id=task_id,
            lease_owner=lease_owner,
            lease_seconds=lease_seconds,
        )

    def _claim_test_task(
        self,
        *,
        task_batch_id: str | None,
        task_id: str | None,
        lease_owner: str,
        lease_seconds: int,
    ) -> dict[str, Any] | None:
        if not lease_owner:
            raise ContractError("lease_owner must not be empty")
        if lease_seconds < 1:
            raise ContractError("lease_seconds must be at least 1")
        now = self._clock.now()
        expires = (
            datetime.fromisoformat(now.replace("Z", "+00:00"))
            + timedelta(seconds=lease_seconds)
        ).astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "UPDATE test_tasks SET status='pending', lease_owner=NULL, "
                    "lease_expires_at=NULL, updated_at=? "
                    "WHERE status IN ('leased','generating','preprocessing','evaluating','syncing') "
                    "AND lease_expires_at IS NOT NULL "
                    "AND lease_expires_at <= ?",
                    (now, now),
                )
                clauses = ["status='pending'"]
                params: list[Any] = []
                if task_batch_id:
                    clauses.append("task_batch_id=?")
                    params.append(task_batch_id)
                if task_id:
                    clauses.append("task_id=?")
                    params.append(task_id)
                row = self._conn.execute(
                    "SELECT task_id FROM test_tasks WHERE "
                    + " AND ".join(clauses)
                    + " ORDER BY task_id LIMIT 1",
                    params,
                ).fetchone()
                if row is None:
                    self._conn.commit()
                    return None
                claimed_id = row["task_id"]
                self._conn.execute(
                    "UPDATE test_tasks SET status='leased', lease_owner=?, "
                    "lease_expires_at=?, attempt_count=attempt_count+1, updated_at=? "
                    "WHERE task_id=? AND status='pending'",
                    (lease_owner, expires, now, claimed_id),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return self.get_test_task(claimed_id)

    def update_test_task(
        self,
        task_id: str,
        status: str,
        *,
        lease_owner: str | None = None,
        result_refs: dict[str, Any] | None = None,
        last_error: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        allowed = {
            "pending", "leased", "generating", "preprocessing", "evaluating",
            "syncing", "completed", "error", "cancelled"
        }
        if status not in allowed:
            raise ContractError(f"invalid test task status: {status}")
        current = self.get_test_task(task_id)
        if current["status"] in {"completed", "cancelled"} and status != current["status"]:
            raise StateError(
                f"terminal test task {task_id} cannot transition from {current['status']} to {status}"
            )
        if lease_owner and current.get("lease_owner") not in {None, lease_owner}:
            raise ConflictError(f"test task {task_id} is leased by another worker")
        merged_refs = {**current.get("result_refs", {}), **(result_refs or {})}
        terminal = status in {"completed", "error", "cancelled"}
        with self._conn:
            self._conn.execute(
                "UPDATE test_tasks SET status=?, result_refs=?, last_error=?, "
                "lease_owner=?, lease_expires_at=?, updated_at=? WHERE task_id=?",
                (
                    status,
                    _json(merged_refs),
                    _json(last_error) if last_error is not None else None,
                    None if terminal else current.get("lease_owner"),
                    None if terminal else current.get("lease_expires_at"),
                    self._clock.now(),
                    task_id,
                ),
            )
        return self.get_test_task(task_id)

    def test_task_summary(self, task_batch_id: str) -> dict[str, Any]:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS count FROM test_tasks "
            "WHERE task_batch_id=? GROUP BY status ORDER BY status",
            (task_batch_id,),
        ).fetchall()
        counts = {row["status"]: int(row["count"]) for row in rows}
        return {
            "task_batch_id": task_batch_id,
            "total": sum(counts.values()),
            "counts": counts,
        }

    # ------------------------------------------------------------------
    # generation runs
    # ------------------------------------------------------------------
    def create_run(self, run: dict[str, Any]) -> None:
        run_id = run.get("run_id")
        if not run_id:
            raise ContractError("generation run requires run_id")
        with self._conn:
            self._conn.execute(
                "INSERT INTO generation_runs(run_id, run_batch_id, case_id, case_number, "
                "model_id, mode, origin, status, provenance, result_asset_id, "
                "edited_video_asset_id, expected_audio_source_asset_id, metrics, "
                "raw_events_uri, updated_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    run.get("run_batch_id", ""),
                    run.get("case_id", ""),
                    run.get("case_number", ""),
                    run.get("model_id", ""),
                    run.get("mode", ""),
                    run.get("origin", ""),
                    run.get("status", "planned"),
                    _json(run.get("provenance", {})),
                    run.get("result_asset_id"),
                    run.get("edited_video_asset_id"),
                    run.get("expected_audio_source_asset_id"),
                    _json(run.get("metrics", {})),
                    run.get("raw_events_uri"),
                    utc_now(),
                    utc_now(),
                ),
            )
            self.append_event(
                run_id,
                "run_created",
                payload={
                    "case_id": run.get("case_id"),
                    "run_batch_id": run.get("run_batch_id"),
                    "model_id": run.get("model_id"),
                    "mode": run.get("mode"),
                },
            )

    def update_run_status(
        self,
        run_id: str,
        status: str,
        *,
        metrics: dict[str, Any] | None = None,
        result_asset_id: str | None = None,
        raw_events_uri: str | None = None,
        expected_status: str | None = None,
    ) -> dict[str, Any]:
        current = self.get_run(run_id)
        if current["status"] in TERMINAL_STATUSES and status not in TERMINAL_STATUSES:
            raise StateError(
                f"run {run_id} is already terminal ({current['status']}); "
                "terminal states never return to running"
            )
        if expected_status is not None and current["status"] != expected_status:
            raise ConflictError(
                f"run {run_id} status {current['status']!r} != expected "
                f"{expected_status!r}"
            )
        with self._conn:
            self._conn.execute(
                "UPDATE generation_runs SET status=?, metrics=?, result_asset_id=?, "
                "raw_events_uri=?, updated_at=? WHERE run_id=?",
                (
                    status,
                    _json(metrics or current.get("metrics", {})),
                    result_asset_id if result_asset_id is not None else current.get("result_asset_id"),
                    raw_events_uri if raw_events_uri is not None else current.get("raw_events_uri"),
                    utc_now(),
                    run_id,
                ),
            )
            self.append_event(run_id, f"status_{status}", payload={"metrics": metrics or {}})
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT * FROM generation_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"generation run not found: {run_id}", entity_id=run_id)
        return self._run_dict(row)

    def list_runs(
        self,
        *,
        run_batch_id: str | None = None,
        case_id: str | None = None,
        status: str | None = None,
        model_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        for key, value in (
            ("run_batch_id", run_batch_id),
            ("case_id", case_id),
            ("status", status),
            ("model_id", model_id),
        ):
            if value:
                clauses.append(f"{key} = ?")
                params.append(value)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self._conn.execute(
            f"SELECT * FROM generation_runs{where} ORDER BY case_number, run_id", params
        ).fetchall()
        return [self._run_dict(row) for row in rows]

    def max_case_suffix(self, prefix: str) -> int:
        """Return the largest numeric suffix already used for a case prefix."""

        rows = self._conn.execute(
            "SELECT case_number FROM generation_runs WHERE case_number LIKE ?",
            (prefix + "%",),
        ).fetchall()
        maximum = 0
        for row in rows:
            suffix = row["case_number"][len(prefix) :]
            if suffix == "":
                # The first unsuffixed attempt occupies logical slot 1;
                # a later attempt must continue at _02.
                maximum = max(maximum, 1)
            if suffix.startswith("_") and suffix[1:].isdigit():
                maximum = max(maximum, int(suffix[1:]))
        return maximum

    def set_run_batch_id(self, run_id: str, run_batch_id: str) -> None:
        with self._lock:
            with self._conn:
                cursor = self._conn.execute(
                    "UPDATE generation_runs SET run_batch_id = ? WHERE run_id = ?",
                    (run_batch_id, run_id),
                )
                if cursor.rowcount == 0:
                    raise NotFoundError(f"generation run not found: {run_id}", entity_id=run_id)

    # ------------------------------------------------------------------
    # preprocess runs
    # ------------------------------------------------------------------
    def upsert_preprocess_run(self, preprocess: dict[str, Any]) -> None:
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO preprocess_runs(preprocess_id, run_id, input_hash, "
                    "config_hash, producer_version, payload, status, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(input_hash, config_hash, producer_version) "
                    "DO UPDATE SET payload=excluded.payload, status=excluded.status",
                    (
                        preprocess.get("preprocess_id"),
                        preprocess.get("run_id"),
                        preprocess.get("input_hash", ""),
                        preprocess.get("config_hash", ""),
                        preprocess.get("producer_version", ""),
                        _json(preprocess),
                        preprocess.get("status", "completed"),
                        preprocess.get("created_at", utc_now()),
                    ),
                )

    def find_preprocess_run(
        self, input_hash: str, config_hash: str, producer_version: str
    ) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT payload FROM preprocess_runs WHERE input_hash=? AND config_hash=? "
            "AND producer_version=?",
            (input_hash, config_hash, producer_version),
        ).fetchone()
        return json.loads(row["payload"]) if row is not None else None

    def get_preprocess_run(self, preprocess_id: str) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT payload FROM preprocess_runs WHERE preprocess_id = ?",
            (preprocess_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"preprocess run not found: {preprocess_id}")
        return json.loads(row["payload"])

    def get_preprocess_for_run(self, run_id: str) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT payload FROM preprocess_runs WHERE run_id = ? AND status = 'completed' "
            "ORDER BY created_at DESC, preprocess_id DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(
                f"preprocess result missing for run {run_id}; run preprocess first",
                entity_id=run_id,
            )
        return json.loads(row["payload"])

    # ------------------------------------------------------------------
    # judgments and evaluation results
    # ------------------------------------------------------------------
    def append_judgment(self, judgment: dict[str, Any]) -> None:
        key = (
            judgment.get("evaluation_id"),
            judgment.get("dimension_id"),
            judgment.get("judge_id"),
            judgment.get("judge_version"),
        )
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "INSERT OR REPLACE INTO judgments(evaluation_id, dimension_id, judge_id, "
                    "judge_version, run_id, payload, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        key[0],
                        key[1],
                        key[2],
                        key[3],
                        judgment.get("run_id", ""),
                        _json(judgment),
                        utc_now(),
                    ),
                )

    def list_judgments(self, evaluation_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT payload FROM judgments WHERE evaluation_id = ? "
            "ORDER BY dimension_id, judge_id",
            (evaluation_id,),
        ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def save_evaluation_result(self, result: dict[str, Any]) -> None:
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO evaluation_results(evaluation_id, evaluation_batch_id, "
                    "run_id, benchmark_version, payload, created_at) VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(evaluation_id) DO UPDATE SET payload=excluded.payload",
                    (
                        result.get("evaluation_id"),
                        result.get("evaluation_batch_id", ""),
                        result.get("run_id", ""),
                        result.get("benchmark_version", ""),
                        _json(result),
                        utc_now(),
                    ),
                )

    def get_evaluation_result(self, evaluation_id: str) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT payload FROM evaluation_results WHERE evaluation_id = ?",
            (evaluation_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"evaluation result not found: {evaluation_id}")
        return json.loads(row["payload"])

    def list_evaluation_results(
        self,
        *,
        run_id: str | None = None,
        evaluation_batch_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        for key, value in (("run_id", run_id), ("evaluation_batch_id", evaluation_batch_id)):
            if value:
                clauses.append(f"{key} = ?")
                params.append(value)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self._conn.execute(
            f"SELECT payload FROM evaluation_results{where} ORDER BY evaluation_id", params
        ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def latest_evaluation_result(
        self, run_id: str, benchmark_version: str | None = None
    ) -> dict[str, Any] | None:
        params: list[Any] = [run_id]
        clause = "run_id = ?"
        if benchmark_version:
            clause += " AND benchmark_version = ?"
            params.append(benchmark_version)
        row = self._conn.execute(
            f"SELECT payload FROM evaluation_results WHERE {clause} "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            params,
        ).fetchone()
        return json.loads(row["payload"]) if row is not None else None

    # ------------------------------------------------------------------
    # human signals and proposals
    # ------------------------------------------------------------------
    def append_human_signal(self, signal: dict[str, Any]) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO human_signals(signal_id, sample_id, source_type, "
                "raw_text, payload, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    signal.get("signal_id"),
                    signal.get("sample_id", ""),
                    signal.get("source_type", ""),
                    signal.get("raw_text", ""),
                    _json(signal),
                    utc_now(),
                ),
            )

    def get_human_signal(self, signal_id: str) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT payload FROM human_signals WHERE signal_id = ?", (signal_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"human signal not found: {signal_id}")
        return json.loads(row["payload"])

    def list_human_signals(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT payload FROM human_signals ORDER BY created_at, signal_id"
        ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def update_human_signal(self, signal: dict[str, Any]) -> None:
        """Update derived fields while preserving immutable raw text."""

        current = self.get_human_signal(signal["signal_id"])
        if signal.get("raw_text") != current.get("raw_text"):
            raise ContractError("human signal raw_text is immutable")
        payload = {**signal, "raw_text": current.get("raw_text", "")}
        with self._conn:
            self._conn.execute(
                "UPDATE human_signals SET payload = ? WHERE signal_id = ?",
                (_json(payload), signal["signal_id"]),
            )

    def save_proposal(self, proposal: dict[str, Any]) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO dimension_proposals(proposal_id, status, payload, created_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(proposal_id) DO UPDATE SET "
                "status=excluded.status, payload=excluded.payload",
                (
                    proposal.get("proposal_id"),
                    proposal.get("status", "proposed"),
                    _json(proposal),
                    utc_now(),
                ),
            )

    def get_proposal(self, proposal_id: str) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT payload FROM dimension_proposals WHERE proposal_id = ?",
            (proposal_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"proposal not found: {proposal_id}")
        return json.loads(row["payload"])

    # ------------------------------------------------------------------
    # judge releases
    # ------------------------------------------------------------------
    def record_judge_release(
        self, judge_id: str, version: str, status: str, payload: dict[str, Any]
    ) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO judge_releases(judge_id, version, status, "
                "payload, created_at) VALUES (?, ?, ?, ?, ?)",
                (judge_id, version, status, _json(payload), utc_now()),
            )

    def get_judge_release(self, judge_id: str, version: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT payload FROM judge_releases WHERE judge_id = ? AND version = ?",
            (judge_id, version),
        ).fetchone()
        return json.loads(row["payload"]) if row is not None else None

    # ------------------------------------------------------------------
    # approvals
    # ------------------------------------------------------------------
    def save_approval(self, approval: dict[str, Any]) -> None:
        approval_hash = approval.get("approval_hash")
        if not approval_hash:
            raise ContractError("approval requires approval_hash")
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO approvals(approval_id, approval_hash, operator, "
                "scope, payload, approved_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    approval.get("approval_id"),
                    approval_hash,
                    approval.get("operator", ""),
                    approval.get("scope", ""),
                    _json(approval.get("payload", {})),
                    approval.get("approved_at", utc_now()),
                ),
            )

    def find_approval_by_hash(self, approval_hash: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT payload FROM approvals WHERE approval_hash = ?", (approval_hash,)
        ).fetchone()
        if row is None:
            return None
        data = json.loads(row["payload"])
        data["approval_id"] = self._conn.execute(
            "SELECT approval_id FROM approvals WHERE approval_hash = ?", (approval_hash,)
        ).fetchone()["approval_id"]
        return data

    # ------------------------------------------------------------------
    # sync ledger
    # ------------------------------------------------------------------
    def upsert_sync_ledger(
        self,
        entity_type: str,
        entity_id: str,
        destination: str,
        *,
        payload_hash: str,
        sync_status: str = "pending",
        feishu_record_id: str | None = None,
        last_error: str | None = None,
    ) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO sync_ledger(entity_type, entity_id, destination, payload_hash, "
                "sync_status, feishu_record_id, attempt_count, last_error, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?) "
                "ON CONFLICT(entity_type, entity_id, destination) DO UPDATE SET "
                "payload_hash=excluded.payload_hash, sync_status=excluded.sync_status, "
                "feishu_record_id=excluded.feishu_record_id, last_error=excluded.last_error, "
                "updated_at=excluded.updated_at",
                (
                    entity_type,
                    entity_id,
                    destination,
                    payload_hash,
                    sync_status,
                    feishu_record_id,
                    last_error,
                    utc_now(),
                ),
            )

    def update_sync_ledger(
        self,
        entity_type: str,
        entity_id: str,
        destination: str,
        *,
        sync_status: str | None = None,
        feishu_record_id: str | None = None,
        last_error: str | None = None,
        increment_attempts: bool = False,
    ) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE sync_ledger SET sync_status=COALESCE(?, sync_status), "
                "feishu_record_id=COALESCE(?, feishu_record_id), "
                "last_error=COALESCE(?, last_error), "
                "attempt_count=attempt_count + ?, updated_at=? "
                "WHERE entity_type=? AND entity_id=? AND destination=?",
                (
                    sync_status,
                    feishu_record_id,
                    last_error,
                    1 if increment_attempts else 0,
                    utc_now(),
                    entity_type,
                    entity_id,
                    destination,
                ),
            )

    def list_sync_ledger(
        self, *, entity_type: str | None = None, sync_status: str | None = None
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        for key, value in (("entity_type", entity_type), ("sync_status", sync_status)):
            if value:
                clauses.append(f"{key} = ?")
                params.append(value)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self._conn.execute(
            f"SELECT * FROM sync_ledger{where} ORDER BY entity_type, entity_id", params
        ).fetchall()
        return [self._row_dict(row) for row in rows]

    # ------------------------------------------------------------------
    # stage runs and batch manifests
    # ------------------------------------------------------------------
    def save_stage_run(self, manifest: dict[str, Any]) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO stage_runs(stage_run_id, stage, status, input_hash, "
                "config_hash, producer_version, payload, started_at, completed_at, "
                "attempt_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(stage_run_id) DO UPDATE SET status=excluded.status, "
                "payload=excluded.payload, completed_at=excluded.completed_at",
                (
                    manifest.get("stage_run_id"),
                    manifest.get("stage", ""),
                    manifest.get("status", "planned"),
                    manifest.get("input_hash", ""),
                    manifest.get("config_hash", ""),
                    manifest.get("producer_version", ""),
                    _json(manifest),
                    manifest.get("started_at", utc_now()),
                    manifest.get("completed_at"),
                    int(manifest.get("attempt_count", 1)),
                ),
            )

    def get_stage_run(self, stage_run_id: str) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT payload FROM stage_runs WHERE stage_run_id = ?", (stage_run_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"stage run not found: {stage_run_id}")
        return json.loads(row["payload"])

    def find_stage_run(
        self, stage: str, input_hash: str, config_hash: str, producer_version: str
    ) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT payload FROM stage_runs WHERE stage=? AND input_hash=? AND "
            "config_hash=? AND producer_version=? AND status='completed' "
            "ORDER BY started_at DESC LIMIT 1",
            (stage, input_hash, config_hash, producer_version),
        ).fetchone()
        return json.loads(row["payload"]) if row is not None else None

    def save_batch_manifest(self, manifest: dict[str, Any]) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO batch_manifests(entity_type, batch_id, content_hash, "
                "payload, created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    manifest.get("entity_type"),
                    manifest.get("batch_id"),
                    manifest.get("content_hash", ""),
                    _json(manifest),
                    manifest.get("created_at", utc_now()),
                ),
            )

    def get_batch_manifest(self, entity_type: str, batch_id: str) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT payload FROM batch_manifests WHERE entity_type=? AND batch_id=?",
            (entity_type, batch_id),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"batch manifest not found: {entity_type}/{batch_id}")
        return json.loads(row["payload"])

    def list_batch_manifests(self, entity_type: str | None = None) -> list[dict[str, Any]]:
        if entity_type:
            rows = self._conn.execute(
                "SELECT payload FROM batch_manifests WHERE entity_type=? "
                "ORDER BY created_at, batch_id",
                (entity_type,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT payload FROM batch_manifests ORDER BY entity_type, batch_id"
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    # ------------------------------------------------------------------
    # selector snapshots and result imports
    # ------------------------------------------------------------------
    def save_selector_snapshot(self, selector: dict[str, Any]) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO selector_snapshots(selector_id, snapshot_hash, "
                "payload, snapshot_at) VALUES (?, ?, ?, ?)",
                (
                    selector.get("selector_id"),
                    selector.get("snapshot_hash"),
                    _json(selector),
                    selector.get("snapshot_at", utc_now()),
                ),
            )

    def get_selector_snapshot(self, selector_id: str, snapshot_hash: str) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT payload FROM selector_snapshots WHERE selector_id=? AND snapshot_hash=?",
            (selector_id, snapshot_hash),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"selector snapshot not found: {selector_id}")
        return json.loads(row["payload"])

    def save_result_import(
        self, import_request_id: str, source_hash: str, payload: dict[str, Any]
    ) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO result_imports(import_request_id, source_hash, "
                "payload, imported_at) VALUES (?, ?, ?, ?)",
                (import_request_id, source_hash, _json(payload), utc_now()),
            )

    def get_result_import(self, import_request_id: str, source_hash: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT payload FROM result_imports WHERE import_request_id=? AND source_hash=?",
            (import_request_id, source_hash),
        ).fetchone()
        return json.loads(row["payload"]) if row is not None else None

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {key: row[key] for key in row.keys()}

    @staticmethod
    def _asset_dict(row: sqlite3.Row) -> dict[str, Any]:
        data = {
            "asset_id": row["asset_id"],
            "kind": row["kind"],
            "uri": row["uri"],
            "sha256": row["sha256"],
            "bytes": row["bytes"],
            "mime_type": row["mime_type"],
            "status": row["status"],
            "created_at": row["created_at"],
        }
        for key in ("source", "media", "metadata"):
            data[key] = json.loads(row[key])
        return data

    @staticmethod
    def _run_dict(row: sqlite3.Row) -> dict[str, Any]:
        data = {
            "run_id": row["run_id"],
            "run_batch_id": row["run_batch_id"],
            "case_id": row["case_id"],
            "case_number": row["case_number"],
            "model_id": row["model_id"],
            "mode": row["mode"],
            "origin": row["origin"],
            "status": row["status"],
            "result_asset_id": row["result_asset_id"],
            "edited_video_asset_id": row["edited_video_asset_id"],
            "expected_audio_source_asset_id": row["expected_audio_source_asset_id"],
            "raw_events_uri": row["raw_events_uri"],
        }
        data["provenance"] = json.loads(row["provenance"])
        data["metrics"] = json.loads(row["metrics"])
        return data

    @staticmethod
    def _task_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "task_id": row["task_id"],
            "task_batch_id": row["task_batch_id"],
            "plan_id": row["plan_id"],
            "case_id": row["case_id"],
            "status": row["status"],
            "payload": json.loads(row["payload"]),
            "lease_owner": row["lease_owner"],
            "lease_expires_at": row["lease_expires_at"],
            "attempt_count": int(row["attempt_count"]),
            "result_refs": json.loads(row["result_refs"]),
            "last_error": json.loads(row["last_error"]) if row["last_error"] else None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def close(self) -> None:
        self._conn.close()
