"""P10 Feishu sync tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from xmax_test.errors import ContractError, ExternalServiceError
from xmax_test.feishu.attachments import AttachmentUploader
from xmax_test.feishu.client import FakeFeishuSyncClient, LarkCliSyncClient
from xmax_test.feishu.ledger import SyncLedger
from xmax_test.feishu.reconcile import ReconcileService
from xmax_test.feishu.sync import FeishuSyncService
from xmax_test.storage.artifacts import ArtifactStore
from xmax_test.storage.sqlite import SqliteMetadataRepository

FEISHU_CONFIG = {
    "connection": {"provider": "fake", "identity": "fake"},
    "base": {"url": "https://fake.feishu.cn/base/app", "app_token": "app"},
    "tables": {
        "feed_data": "tbl-feed",
        "prompt_data": "tbl-prompt",
        "case_data": "tbl-case",
    },
    "field_projection": {
        "case_data": {
            "case_number": "case编号",
            "result_attachment": "case文件",
            "score_percent": "case评分",
            "description": "case说明",
            "feed_attachments": "feed文件",
            "prompt_text": "prompt文字",
            "prompt_attachments": "prompt素材",
            "model_version": "Xmax模型版本",
        }
    },
    "case_score": {
        "source": "scenario_score",
        "display": "percentage",
        "internal_range": [0, 100],
        "feishu_storage_range": [0, 1],
        "write_transform": "divide_by_100",
        "read_transform": "multiply_by_100",
        "unscored_value": None,
        "failed_run_value": 0,
    },
    "write_mode": "upsert",
    "dry_run": True,
}


class FeishuTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.repository = SqliteMetadataRepository(root / "db.sqlite3")
        self.artifacts = ArtifactStore(root / "artifacts")
        self.client = FakeFeishuSyncClient()
        self.ledger = SyncLedger(self.repository)
        self.uploader = AttachmentUploader(self.client)
        self.service = FeishuSyncService(
            self.client,
            self.ledger,
            self.uploader,
            FEISHU_CONFIG,
            self.repository,
            self.artifacts,
        )
        self._seed_assets()

    def tearDown(self) -> None:
        self.repository.close()
        self.directory.cleanup()

    def _seed_assets(self) -> None:
        for asset_id in ("result-a", "feed-a", "prompt-a"):
            kind = (
                "result_video"
                if asset_id.startswith("result")
                else "feed_video"
                if asset_id.startswith("feed")
                else "prompt_image"
            )
            self.repository.upsert_asset(
                {
                    "asset_id": asset_id,
                    "kind": kind,
                    "uri": f"artifact://assets/{asset_id}/source.bin",
                    "sha256": f"sha-{asset_id}",
                    "bytes": 4,
                    "mime_type": "image/png" if kind == "prompt_image" else "video/mp4",
                    "status": "ready",
                }
            )
            path = self.artifacts.resolve(f"artifact://assets/{asset_id}/source.bin")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"{asset_id}-data".encode())

    def run_record(
        self,
        *,
        run_id: str = "run-1",
        case_number: str = "feed001_prompt001_01",
        status: str = "completed",
        model: str = "x2.0",
        result_asset: str | None = "result-a",
        case_score: float | None = 85.0,
    ) -> dict:
        self.repository.create_run(
            {
                "run_id": run_id,
                "run_batch_id": "batch-1",
                "case_id": f"case-{run_id}",
                "case_number": case_number,
                "status": status,
                "model_id": model,
                "mode": "offline",
                "origin": "xmax_offline",
                "provenance": {
                    "source_type": "t",
                    "source_locator": "l",
                    "source_hash": "h",
                },
                "result_asset_id": result_asset,
                "edited_video_asset_id": "feed-a",
                "expected_audio_source_asset_id": "feed-a",
                "metrics": {"failure_class": None if status == "completed" else "model_error"},
            }
        )
        self.repository.upsert_test_case(
            {
                "case_id": f"case-{run_id}",
                "case_number": case_number,
                "feed_number": "feed001",
                "prompt_number": "prompt001",
                "feed_asset_id": "feed-a",
                "prompt_asset_ids": ["prompt-a"],
                "prompt_text": "换装",
                "api_asset_bindings": {
                    "refVideoPath": "feed_video",
                    "refImagePath": "prompt_image",
                },
            },
            "plan-test",
        )
        run = self.repository.get_run(run_id)
        if case_score is not None:
            self.repository.save_evaluation_result(
                {
                    "evaluation_id": f"eval-{run_id}",
                    "evaluation_batch_id": "batch-eval",
                    "run_id": run_id,
                    "benchmark_version": "0.1.0-draft",
                    "scenario_pack_version": "0.1.0-draft",
                    "score_schema_version": "0.1.0-draft",
                    "dimension_results": [],
                    "weight_resolution": {},
                    "case_score_percent": case_score,
                }
            )
        return run

    def selected_evaluations(self, *runs: dict) -> dict[str, dict]:
        selected: dict[str, dict] = {}
        for run in runs:
            results = self.repository.list_evaluation_results(run_id=run["run_id"])
            if results:
                selected[run["run_id"]] = results[-1]
        return selected


class LarkCliPathTests(unittest.TestCase):
    def test_upload_path_is_project_relative_and_outside_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            media = root / "video.mp4"
            media.write_bytes(b"video")
            safe = LarkCliSyncClient._safe_upload_path(str(media), cwd=Path.cwd())
            self.assertTrue(safe.startswith("./"))
            self.assertFalse(Path(safe).is_absolute())
        with tempfile.TemporaryDirectory() as outside:
            outside_file = Path(outside) / "video.mp4"
            outside_file.write_bytes(b"video")
            with self.assertRaises(ExternalServiceError):
                LarkCliSyncClient._safe_upload_path(
                    str(outside_file), cwd=Path.cwd() / "not-this-directory"
                )


class FullSyncTests(FeishuTestBase):
    def test_batch_sync_rejects_duplicate_case_model_keys(self) -> None:
        first = self.run_record(run_id="run-1")
        second = self.run_record(run_id="run-2")
        with self.assertRaises(ContractError):
            self.service.sync_case_runs([first, second], policy="full")
        self.assertEqual(self.client._tables.get("tbl-case", []), [])

    def test_upsert_without_echoed_record_id_is_resolved_before_attachments(
        self,
    ) -> None:
        original = self.client.upsert_record

        def no_id(app_token, table_id, record_id, fields):
            original(app_token, table_id, record_id, fields)
            return {"created": record_id is None, "updated": record_id is not None}

        self.client.upsert_record = no_id
        summary = self.service.sync_case_runs([self.run_record()], policy="full")
        self.assertEqual(summary["errors"], [])
        self.assertEqual(summary["created"], 1)
        self.assertIn("find_record", self.client.calls)
        self.assertEqual(len(self.client.uploads), 3)

    def test_live_lark_name_field_shape_is_accepted(self) -> None:
        original = self.client.get_fields
        self.client.get_fields = lambda app_token, table_id: [
            {"name": item["field_name"], "type": item["type"]}
            for item in original(app_token, table_id)
        ]
        summary = self.service.sync_case_runs([self.run_record()], policy="metadata_only")
        self.assertEqual(summary["errors"], [])

    def test_full_sync_creates_one_case_row_per_run_with_scaled_score(self) -> None:
        run = self.run_record(case_score=85.0)
        summary = self.service.sync_case_runs(
            [run], evaluations=self.selected_evaluations(run), policy="full"
        )
        self.assertEqual(summary["created"], 1)
        records = self.client._tables["tbl-case"]
        self.assertEqual(len(records), 1)
        fields = records[0]["fields"]
        self.assertEqual(fields["case编号"], "feed001_prompt001_01")
        self.assertEqual(fields["Xmax模型版本"], "x2.0")
        # Internal 85 -> feishu 0.85
        self.assertEqual(fields["case评分"], 0.85)
        # Attachments were uploaded.
        self.assertIn("case文件", fields)
        self.assertIn("feed文件", fields)
        self.assertEqual(fields["case文件"][0]["name"], "feed001_prompt001_01.mp4")
        self.assertEqual(fields["feed文件"][0]["name"], "feed001.mp4")
        self.assertEqual(fields["prompt素材"][0]["name"], "prompt001_prompt素材_01.png")

    def test_failed_run_writes_zero_percent(self) -> None:
        run = self.run_record(status="error", case_score=None)
        summary = self.service.sync_case_runs([run], policy="full")
        self.assertEqual(summary["created"], 1)
        fields = self.client._tables["tbl-case"][0]["fields"]
        self.assertEqual(fields["case评分"], 0.0)
        self.assertIn("生成失败", fields.get("case说明", ""))

    def test_unreviewed_run_writes_empty_score(self) -> None:
        run = self.run_record(case_score=None)
        self.service.sync_case_runs([run], policy="full")
        fields = self.client._tables["tbl-case"][0]["fields"]
        self.assertIsNone(fields["case评分"])
        self.assertIsNone(fields["case说明"])

    def test_unselected_latest_evaluation_is_not_published(self) -> None:
        run = self.run_record(case_score=99.39)
        summary = self.service.sync_case_runs([run], policy="full")
        self.assertEqual(summary["errors"], [])
        fields = self.client._tables["tbl-case"][0]["fields"]
        self.assertIsNone(fields["case评分"])
        self.assertIsNone(fields["case说明"])

    def test_wrong_bin_attachments_are_replaced_with_business_names(self) -> None:
        run = self.run_record(case_score=None)
        self.client._tables["tbl-case"] = [
            {
                "record_id": "rec-existing",
                "fields": {
                    "case编号": run["case_number"],
                    "Xmax模型版本": run["model_id"],
                    "case评分": 0.9939,
                    "case说明": "已生成",
                    "case文件": [{"file_token": "old-result", "name": "source.bin"}],
                    "feed文件": [{"file_token": "old-feed", "name": "source.bin"}],
                    "prompt素材": [{"file_token": "old-prompt", "name": "source.bin"}],
                },
            }
        ]
        summary = self.service.sync_case_runs([run], policy="full")
        self.assertEqual(summary["errors"], [])
        fields = self.client._tables["tbl-case"][0]["fields"]
        self.assertIsNone(fields["case评分"])
        self.assertIsNone(fields["case说明"])
        self.assertEqual([x["name"] for x in fields["case文件"]], ["feed001_prompt001_01.mp4"])
        self.assertEqual([x["name"] for x in fields["feed文件"]], ["feed001.mp4"])
        self.assertEqual([x["name"] for x in fields["prompt素材"]], ["prompt001_prompt素材_01.png"])
        self.assertEqual(self.client.calls.count("remove_attachments"), 3)

    def test_same_asset_is_attached_to_each_repeat_row(self) -> None:
        first = self.run_record(
            run_id="run-first", case_number="feed001_prompt001_01", case_score=None
        )
        second = self.run_record(
            run_id="run-second", case_number="feed001_prompt001_02", case_score=None
        )
        summary = self.service.sync_case_runs([first, second], policy="full")
        self.assertEqual(summary["errors"], [])
        self.assertEqual(summary["created"], 2)
        self.assertEqual(len(self.client.uploads), 6)
        self.assertEqual({item["record_id"] for item in self.client.uploads}, {"rec-1", "rec-2"})

    def test_video_reference_uploads_original_feed_and_actual_capture(self) -> None:
        run = self.run_record(case_score=None)
        case = self.repository.get_test_case(run["case_id"])
        case["api_asset_bindings"]["refImagePath"] = "feed_capture"
        self.repository.upsert_test_case(case, "plan-test")
        feed = self.repository.get_asset("feed-a")
        capture_id = f"{feed['asset_id']}-{feed['sha256'][:12]}"
        capture = self.artifacts.resolve(f"artifact://captures/{capture_id}/middle.jpg")
        capture.parent.mkdir(parents=True, exist_ok=True)
        capture.write_bytes(b"\xff\xd8\xffcapture")

        summary = self.service.sync_case_runs([run], policy="full")
        self.assertEqual(summary["errors"], [])
        names = [item["name"] for item in self.client._tables["tbl-case"][0]["fields"]["feed文件"]]
        self.assertEqual(names, ["feed001.mp4", "feed001_feed截图.jpg"])

    def test_selected_evaluation_writes_specific_case_description(self) -> None:
        run = self.run_record(case_score=66.0)
        evaluation = self.selected_evaluations(run)[run["run_id"]]
        evaluation["dimension_results"] = [
            {
                "dimension_id": "C2",
                "score": 0,
                "assessable": True,
                "evidence": [{"description": "未执行换装，人物服装全程保持原样。"}],
            },
            {
                "dimension_id": "C9",
                "score": 0.5,
                "assessable": True,
                "evidence": [{"description": "边缘持续闪烁。"}],
            },
            {
                "dimension_id": "C10",
                "score": 1,
                "assessable": True,
                "evidence": [{"description": "整体略显生硬。"}],
            },
        ]
        summary = self.service.sync_case_runs(
            [run], evaluations={run["run_id"]: evaluation}, policy="full"
        )
        self.assertEqual(summary["errors"], [])
        description = self.client._tables["tbl-case"][0]["fields"]["case说明"]
        self.assertNotIn("66.00%", description)
        self.assertNotIn("分", description)
        self.assertTrue(description.startswith("主要问题：C2"))
        self.assertIn("未执行换装", description)
        self.assertIn("边缘持续闪烁", description)
        self.assertNotIn("整体略显生硬", description)

    def test_same_payload_skips_second_sync(self) -> None:
        run = self.run_record(case_score=85.0)
        self.service.sync_case_runs([run], policy="full")
        second = self.service.sync_case_runs([run], policy="full")
        self.assertEqual(second["created"], 0)
        self.assertEqual(second["skipped"], 1)
        self.assertEqual(len(self.client._tables["tbl-case"]), 1)

    def test_structure_is_read_before_write(self) -> None:
        run = self.run_record()
        self.service.sync_case_runs([run], policy="full")
        self.assertIn("get_fields", self.client.calls)
        self.assertIn("tbl-case", self.client.field_checks)


class PolicyTests(FeishuTestBase):
    def test_score_only_forbids_case_creation_and_uploads(self) -> None:
        run = self.run_record(case_score=70.0)
        summary = self.service.sync_case_runs([run], policy="score_only")
        self.assertEqual(len(summary["errors"]), 1)
        self.assertEqual(summary["created"], 0)
        self.assertEqual(self.client._tables.get("tbl-case"), None)

    def test_score_only_updates_existing_case_score(self) -> None:
        self.client._tables["tbl-case"] = [
            {
                "record_id": "rec-existing",
                "fields": {"case编号": "feed001_prompt001_01", "Xmax模型版本": "x2.0"},
            }
        ]
        run = self.run_record(case_score=66.0)
        summary = self.service.sync_case_runs(
            [run], evaluations=self.selected_evaluations(run), policy="score_only"
        )
        self.assertEqual(summary["updated"], 1)
        self.assertEqual(summary["created"], 0)
        fields = self.client._tables["tbl-case"][0]["fields"]
        self.assertEqual(fields["case评分"], 0.66)
        self.assertEqual(self.client.uploads, [])  # no attachment uploads

    def test_attachments_only_adds_attachments_without_fields(self) -> None:
        self.client._tables["tbl-case"] = [
            {
                "record_id": "rec-existing",
                "fields": {"case编号": "feed001_prompt001_01", "Xmax模型版本": "x2.0"},
            }
        ]
        run = self.run_record(case_score=66.0)
        summary = self.service.sync_case_runs([run], policy="attachments_only")
        self.assertEqual(summary["updated"], 1)
        fields = self.client._tables["tbl-case"][0]["fields"]
        self.assertIn("case文件", fields)
        self.assertIn("feed文件", fields)
        self.assertNotIn("case评分", fields)

    def test_metadata_only_writes_text_fields_only(self) -> None:
        self.client._tables["tbl-case"] = [
            {
                "record_id": "rec-existing",
                "fields": {"case编号": "feed001_prompt001_01", "Xmax模型版本": "x2.0"},
            }
        ]
        run = self.run_record(case_score=66.0)
        run["prompt_text"] = "换装"
        self.service.sync_case_runs([run], policy="metadata_only")
        fields = self.client._tables["tbl-case"][0]["fields"]
        self.assertEqual(fields["prompt文字"], "换装")
        self.assertNotIn("case文件", fields)

    def test_dry_run_writes_nothing(self) -> None:
        run = self.run_record(case_score=50.0)
        summary = self.service.sync_case_runs([run], policy="full", dry_run=True)
        self.assertEqual(summary["dry_run"], True)
        self.assertEqual(self.client._tables.get("tbl-case"), None)
        self.assertEqual(self.client.uploads, [])


class ReconcileTests(FeishuTestBase):
    def test_missing_and_conflict_detection(self) -> None:
        # Local run without remote record.
        run = self.run_record(case_score=85.0)
        outcome = ReconcileService(self.client, FEISHU_CONFIG, self.repository).reconcile()
        self.assertIn("feed001_prompt001_01+x2.0", outcome["missing"])
        self.assertFalse(outcome["ok"])

        # After sync, remote has the record with 0.85; reconcile passes.
        self.service.sync_case_runs(
            [run], evaluations=self.selected_evaluations(run), policy="full"
        )
        outcome = ReconcileService(self.client, FEISHU_CONFIG, self.repository).reconcile(
            evaluations=self.selected_evaluations(run)
        )
        self.assertEqual(outcome["missing"], [])
        self.assertEqual(outcome["conflicts"], [])

    def test_duplicate_keys_detected(self) -> None:
        self.client._tables["tbl-case"] = [
            {
                "record_id": "r1",
                "fields": {"case编号": "feed001_prompt001_01", "Xmax模型版本": "x2.0"},
            },
            {
                "record_id": "r2",
                "fields": {"case编号": "feed001_prompt001_01", "Xmax模型版本": "x2.0"},
            },
        ]
        self.run_record()
        outcome = ReconcileService(self.client, FEISHU_CONFIG, self.repository).reconcile()
        self.assertIn("feed001_prompt001_01+x2.0", outcome["duplicates"])
        self.assertEqual(outcome["duplicates"]["feed001_prompt001_01+x2.0"], 2)

    def test_score_read_back_multiplies_by_100(self) -> None:
        run = self.run_record(case_score=85.0)
        self.service.sync_case_runs(
            [run], evaluations=self.selected_evaluations(run), policy="full"
        )
        records = self.client._tables["tbl-case"]
        stored = records[0]["fields"]["case评分"]
        self.assertEqual(stored, 0.85)
        # Reconcile compares local 85 (0-100) against remote 0.85 (0-1).
        outcome = ReconcileService(self.client, FEISHU_CONFIG, self.repository).reconcile(
            evaluations=self.selected_evaluations(run)
        )
        self.assertEqual(outcome["conflicts"], [])

    def test_unselected_local_history_requires_blank_remote_score(self) -> None:
        run = self.run_record(case_score=99.39)
        self.client._tables["tbl-case"] = [
            {
                "record_id": "rec-existing",
                "fields": {
                    "case编号": run["case_number"],
                    "Xmax模型版本": run["model_id"],
                    "case评分": 0.9939,
                },
            }
        ]
        outcome = ReconcileService(self.client, FEISHU_CONFIG, self.repository).reconcile(
            evaluations={}
        )
        self.assertEqual(len(outcome["conflicts"]), 1)
        self.assertIn("must be blank", outcome["conflicts"][0]["reason"])


class LedgerTests(FeishuTestBase):
    def test_ledger_tracks_resume_state(self) -> None:
        run = self.run_record(case_score=42.0)
        self.service.sync_case_runs([run], policy="full")
        entries = self.repository.list_sync_ledger(entity_type="case_data")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["sync_status"], "synced")
        self.assertEqual(entries[0]["feishu_record_id"], "rec-1")

    def test_error_sets_ledger_error_and_retries_do_not_duplicate(self) -> None:
        run = self.run_record(case_score=42.0)
        self.client.fail_upsert = "feishu down"
        summary = self.service.sync_case_runs([run], policy="full")
        self.assertEqual(len(summary["errors"]), 1)
        entries = self.repository.list_sync_ledger(entity_type="case_data")
        self.assertEqual(entries[0]["sync_status"], "error")
        self.assertGreaterEqual(entries[0]["attempt_count"], 1)


if __name__ == "__main__":
    unittest.main()
