"""P2.5 Existing-result ingestion tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from xmax_test.assets.registry import AssetRegistry
from xmax_test.assets.sources import FakeFeishuClient
from xmax_test.assets.validator import MediaValidator
from xmax_test.errors import ContractError
from xmax_test.ingest.results import ImportService
from xmax_test.planning.recipes import RecipeResolver
from xmax_test.storage.artifacts import ArtifactStore
from xmax_test.storage.sqlite import SqliteMetadataRepository

ROOT = Path(__file__).resolve().parents[2]


class FakeProbe:
    def probe(self, path: Path):
        if path.stat().st_size == 0:
            raise ContractError("empty")
        return {
            "streams": [{"codec_type": "video", "width": 704, "height": 1280, "avg_frame_rate": "24/1"}],
            "format": {"format_name": "mp4", "duration": "8.0"},
        }


class ImportTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.repository = SqliteMetadataRepository(root / "db.sqlite3")
        self.artifacts = ArtifactStore(root / "artifacts")
        self.registry = AssetRegistry(
            self.repository, self.artifacts, MediaValidator(probe=FakeProbe())
        )
        self.recipes = RecipeResolver(ROOT / "config" / "operation-recipes.json")
        self.service = ImportService(
            self.repository, self.registry, self.recipes, root / "downloads"
        )
        self.download_dir = root / "downloads"
        self.download_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.repository.close()
        self.directory.cleanup()

    def base_config(self, **extra) -> dict:
        config: dict = {
            "import_request_id": "import-1",
            "source": {"kind": "feishu_case_data", "base_token": "app", "table_id": "tbl"},
            "selector": {"selector_id": "s", "state": "request", "entity_type": "source_record", "match": {"filters": {}}},
            "field_mapping": {
                "case_number": "case编号",
                "result_attachment": "case文件",
                "model_version": "Xmax模型版本",
                "feed_attachment": "feed文件",
                "prompt_text": "prompt文字",
                "prompt_attachment": "prompt素材",
            },
            "mode_resolution": {"order": ["explicit_field", "operation_recipe"], "on_unresolved": "error"},
            "download": {"result_video": True, "feed_and_prompt_inputs": True},
            "validation": {"media": True, "content_hash": True, "required_case_context": True},
            "imported_run_status": "completed",
            "write_remote": False,
        }
        config.update(extra)
        return config

    def fake_client(self) -> FakeFeishuClient:
        return FakeFeishuClient(
            bitable_pages=[
                {
                    "page_id": None,
                    "records": [
                        {
                            "record_id": "rec-1",
                            "fields": {
                                "case编号": "feed001_prompt001_01",
                                "Xmax模型版本": "x2.0",
                                "case文件": [{"file_token": "tok-result"}],
                                "feed文件": [{"file_token": "tok-feed"}],
                                "prompt文字": "换装：替换角色",
                                "prompt素材": [{"file_token": "tok-prompt", "name": "ref.jpg"}],
                            },
                        }
                    ],
                    "has_more": False,
                    "page_token": None,
                }
            ],
            attachments={
                "tok-result": b"result-video-bytes",
                "tok-feed": b"feed-video-bytes",
                "tok-prompt": b"prompt-image-bytes",
            },
        )


class FeishuImportTests(ImportTestBase):
    def test_feishu_import_creates_assets_runs_and_batch(self) -> None:
        outcome = self.service.import_results(self.base_config(), feishu_client=self.fake_client())
        self.assertEqual(outcome.status, "completed")
        self.assertEqual(outcome.imported, 1)
        self.assertIsNotNone(outcome.run_batch_id)

        assets = self.repository.list_assets()
        self.assertEqual(len(assets), 4)  # result + feed + prompt + text
        kinds = sorted(asset["kind"] for asset in assets)
        self.assertEqual(
            kinds, ["feed_video", "prompt_image", "prompt_text", "result_video"]
        )

        runs = self.repository.list_runs(run_batch_id=outcome.run_batch_id)
        self.assertEqual(len(runs), 1)
        run = runs[0]
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["origin"], "feishu_import")
        self.assertEqual(run["provenance"]["source_type"], "feishu_case_data")
        self.assertEqual(run["provenance"]["source_record_id"], "rec-1")
        self.assertTrue(run["metrics"].get("unavailable_metrics"))

        case = self.repository.get_test_case(run["case_id"])
        self.assertEqual(case["case_number"], "feed001_prompt001_01")
        self.assertEqual(case["operation_recipe_id"], "offline-image-reference")
        self.assertEqual(case["edited_video_asset_id"], run["edited_video_asset_id"])

    def test_import_is_idempotent_for_same_snapshot(self) -> None:
        client = self.fake_client()
        first = self.service.import_results(self.base_config(), feishu_client=client)
        second = self.service.import_results(self.base_config(), feishu_client=client)
        self.assertEqual(second.status, "skipped")
        self.assertEqual(first.run_batch_id, second.run_batch_id)
        runs = self.repository.list_runs()
        self.assertEqual(len(runs), 1)

    def test_missing_mode_is_an_error(self) -> None:
        config = self.base_config(
            mode_resolution={"order": ["explicit_field"], "on_unresolved": "error"}
        )
        outcome = self.service.import_results(config, feishu_client=self.fake_client())
        self.assertEqual(outcome.imported, 0)
        self.assertTrue(any("generation mode" in e["message"] for e in outcome.errors))

    def test_missing_recipe_is_an_error(self) -> None:
        client = FakeFeishuClient(
            bitable_pages=[
                {
                    "page_id": None,
                    "records": [
                        {
                            "record_id": "rec-2",
                            "fields": {
                                "case编号": "feed001_prompt002_01",
                                "Xmax模型版本": "x2.0",
                                "case文件": [{"file_token": "tok-result"}],
                                "feed文件": [{"file_token": "tok-feed"}],
                                "prompt文字": "无法归类的提示词",
                                "prompt素材": [{"file_token": "tok-prompt", "name": "ref.jpg"}],
                                "generation_mode": "offline",
                            },
                        }
                    ],
                    "has_more": False,
                    "page_token": None,
                }
            ],
            attachments={
                "tok-result": b"result-video-bytes",
                "tok-feed": b"feed-video-bytes",
                "tok-prompt": b"prompt-image-bytes",
            },
        )
        outcome = self.service.import_results(self.base_config(), feishu_client=client)
        self.assertEqual(outcome.imported, 0)
        self.assertTrue(any("operation recipe" in e["message"] for e in outcome.errors))

    def test_import_never_writes_remote(self) -> None:
        client = self.fake_client()
        before = list(client.calls)
        self.service.import_results(self.base_config(), feishu_client=client)
        for call in client.calls:
            self.assertNotIn("upsert", call)
            self.assertNotIn("write", call)
        self.assertNotEqual(len(client.calls), len(before))


class LocalImportTests(ImportTestBase):
    def test_local_directory_import(self) -> None:
        source_dir = Path(self.directory.name) / "local-results"
        source_dir.mkdir(parents=True)
        (source_dir / "feed001_prompt001_01.mp4").write_bytes(b"result-data")
        (source_dir / "feed001_prompt001_01.feed.mp4").write_bytes(b"feed-data")
        (source_dir / "feed001_prompt001_01.prompt.jpg").write_bytes(b"prompt-img")
        (source_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "cases": [
                        {
                            "case_number": "feed001_prompt001_01",
                            "model_version": "x2.0",
                            "prompt_text": "换装：替换角色",
                            "generation_mode": "offline",
                            "result_file": "feed001_prompt001_01.mp4",
                            "feed_file": "feed001_prompt001_01.feed.mp4",
                            "prompt_file": "feed001_prompt001_01.prompt.jpg",
                        }
                    ]
                },
                ensure_ascii=False,
            )
        )
        config = self.base_config(source={"kind": "local_directory", "directory": str(source_dir)})
        outcome = self.service.import_results(config)
        self.assertEqual(outcome.imported, 1)
        self.assertEqual(outcome.status, "completed")
        run = self.repository.list_runs(run_batch_id=outcome.run_batch_id)[0]
        self.assertEqual(run["origin"], "local_import")

    def test_local_without_mode_and_prompt_is_error(self) -> None:
        source_dir = Path(self.directory.name) / "bad-local"
        source_dir.mkdir(parents=True)
        (source_dir / "feed001_prompt003_01.mp4").write_bytes(b"result-data")
        (source_dir / "feed001_prompt003_01.feed.mp4").write_bytes(b"feed-data")
        config = self.base_config(source={"kind": "local_directory", "directory": str(source_dir)})
        outcome = self.service.import_results(config)
        self.assertEqual(outcome.imported, 0)
        self.assertTrue(outcome.errors)


if __name__ == "__main__":
    unittest.main()
