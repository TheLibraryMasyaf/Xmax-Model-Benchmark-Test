"""P0.5 Persistence tests: SQLite repository and migrations."""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from xmax_test.errors import ContractError, DuplicateError, NotFoundError, StateError
from xmax_test.storage.migrations import _checksum, applied_migrations, available_migrations
from xmax_test.storage.sqlite import SqliteMetadataRepository
from xmax_test.time import FixedClock


class MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / "test.sqlite3"

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_migrate_is_idempotent_on_empty_and_existing_databases(self) -> None:
        first = SqliteMetadataRepository(self.database)
        versions = applied_migrations(first._conn)
        self.assertEqual(versions, {v: c for v, _, c in available_migrations()})
        first.close()

        # Reopening the same file applies nothing and stays consistent.
        second = SqliteMetadataRepository(self.database)
        self.assertEqual(
            applied_migrations(second._conn),
            {v: c for v, _, c in available_migrations()},
        )
        second.close()

    def test_changed_migration_content_is_rejected(self) -> None:
        repository = SqliteMetadataRepository(self.database)
        version, _, _ = available_migrations()[0]
        with repository._conn:
            repository._conn.execute(
                "UPDATE schema_migrations SET checksum = ? WHERE version = ?",
                ("deadbeef", version),
            )
        with self.assertRaises(ContractError):
            SqliteMetadataRepository(self.database)
        repository.close()

    def test_checksum_is_stable(self) -> None:
        _, sql, checksum = available_migrations()[0]
        self.assertEqual(checksum, _checksum(sql))


class RunEventTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.repository = SqliteMetadataRepository(
            Path(self.directory.name) / "test.sqlite3", clock=FixedClock()
        )

    def tearDown(self) -> None:
        self.repository.close()
        self.directory.cleanup()

    def test_events_are_append_only_and_rebuild_state(self) -> None:
        self.repository.create_run(
            {
                "run_id": "run-1",
                "run_batch_id": "batch-1",
                "case_id": "case-1",
                "case_number": "feed001_prompt001_01",
                "model_id": "x2.0",
                "mode": "offline",
                "origin": "xmax_offline",
                "status": "planned",
                "provenance": {"source_type": "test", "source_locator": "fake", "source_hash": "h"},
            }
        )
        self.repository.append_event(
            "run-1", "status_running", payload={"attempt": 1}
        )
        self.repository.append_event(
            "run-1", "status_completed", payload={"ok": True}, external_key="task-9"
        )
        # Deduplicated external event does not create a second row.
        self.repository.append_event(
            "run-1", "status_completed", payload={"ok": True}, external_key="task-9"
        )
        log = self.repository.get_event_log("run-1")
        self.assertEqual(len(log), 3)
        self.assertEqual([item["event"] for item in log], ["run_created", "status_running", "status_completed"])
        rebuilt = self.repository.rebuild_run_from_events("run-1")
        self.assertIsNotNone(rebuilt)
        self.assertEqual(rebuilt["status"], "completed")
        self.assertEqual(rebuilt["run_batch_id"], "batch-1")

    def test_terminal_run_cannot_return_to_running(self) -> None:
        self.repository.create_run(
            {
                "run_id": "run-2",
                "run_batch_id": "batch-1",
                "case_id": "case-1",
                "case_number": "feed001_prompt001_01",
                "model_id": "x2.0",
                "mode": "offline",
                "origin": "xmax_offline",
                "status": "planned",
                "provenance": {"source_type": "test", "source_locator": "fake", "source_hash": "h"},
            }
        )
        self.repository.update_run_status("run-2", "completed")
        with self.assertRaises(StateError):
            self.repository.update_run_status("run-2", "running")

    def test_concurrent_event_writes_do_not_lose_rows(self) -> None:
        self.repository.create_run(
            {
                "run_id": "run-concurrent",
                "run_batch_id": "batch-1",
                "case_id": "case-1",
                "case_number": "feed001_prompt001_01",
                "model_id": "x2.0",
                "mode": "offline",
                "origin": "xmax_offline",
                "status": "planned",
                "provenance": {"source_type": "test", "source_locator": "fake", "source_hash": "h"},
            }
        )
        errors: list[Exception] = []

        def writer(index: int) -> None:
            try:
                for _ in range(20):
                    self.repository.append_event(
                        "run-concurrent", "heartbeat", payload={"writer": index}
                    )
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        log = self.repository.get_event_log("run-concurrent")
        self.assertEqual(len(log), 1 + 100)  # run_created + 5*20 heartbeats
        sequences = [item["sequence"] for item in log[1:]]
        self.assertEqual(sequences, sorted(sequences))
        self.assertEqual(len(set(sequences)), len(sequences))

    def test_missing_run_raises_not_found(self) -> None:
        with self.assertRaises(NotFoundError):
            self.repository.get_run("missing-run")

    def test_asset_hash_is_unique(self) -> None:
        self.repository.upsert_asset(
            {
                "asset_id": "asset-a",
                "kind": "feed_video",
                "uri": "artifact://assets/asset-a/source.mp4",
                "sha256": "aa",
                "bytes": 10,
                "status": "ready",
            }
        )
        with self.assertRaises(DuplicateError):
            self.repository.upsert_asset(
                {
                    "asset_id": "asset-b",
                    "kind": "feed_video",
                    "uri": "artifact://assets/asset-b/source.mp4",
                    "sha256": "aa",
                    "bytes": 10,
                    "status": "ready",
                }
            )

    def test_save_test_plan_and_cases(self) -> None:
        plan = {
            "plan_id": "plan-1",
            "plan_version": "1",
            "plan_hash": "hash-1",
            "frozen": True,
            "cases": [
                {
                    "case_id": "case-1",
                    "case_number": "feed001_prompt001_01",
                    "feed_number": "feed001",
                    "prompt_number": "prompt001",
                    "feed_asset_id": "asset-a",
                    "prompt_text": "do it",
                    "generation_mode": "offline",
                    "repeat_index": 1,
                    "model_id": "x2.0",
                    "operation_recipe_id": "r",
                    "operation_recipe_version": "1",
                    "edited_video_asset_id": "asset-a",
                    "expected_audio_source_asset_id": "asset-a",
                }
            ],
        }
        self.repository.save_test_plan(plan)
        self.assertEqual(self.repository.get_test_plan("plan-1", "1")["plan_hash"], "hash-1")
        self.assertEqual(self.repository.get_test_case("case-1")["case_number"], "feed001_prompt001_01")

    def test_sync_ledger_upsert_and_update(self) -> None:
        self.repository.upsert_sync_ledger(
            "case_data", "feed001_prompt001_01", "feishu",
            payload_hash="p1", sync_status="pending",
        )
        self.repository.upsert_sync_ledger(
            "case_data", "feed001_prompt001_01", "feishu",
            payload_hash="p2", sync_status="synced", feishu_record_id="rec-1",
        )
        entries = self.repository.list_sync_ledger(entity_type="case_data")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["payload_hash"], "p2")
        self.assertEqual(entries[0]["feishu_record_id"], "rec-1")

    def test_stage_run_resume_by_hash(self) -> None:
        manifest = {
            "stage_run_id": "stage-1",
            "stage": "plan",
            "status": "completed",
            "input_hash": "in-1",
            "config_hash": "cfg-1",
            "producer_version": "0.1.0",
            "attempt_count": 1,
        }
        self.repository.save_stage_run(manifest)
        found = self.repository.find_stage_run("plan", "in-1", "cfg-1", "0.1.0")
        self.assertEqual(found["stage_run_id"], "stage-1")
        different = self.repository.find_stage_run("plan", "in-2", "cfg-1", "0.1.0")
        self.assertIsNone(different)


if __name__ == "__main__":
    unittest.main()
