from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from xmax_test.feedback.feishu_importer import FeishuHumanDatasetImporter
from xmax_test.feishu.client import FakeFeishuSyncClient


class CapturingMediaImporter:
    def __init__(self) -> None:
        self.records = []

    def import_records(self, records, *, source_file):
        self.records.extend(records)
        return {
            "imported": len(records),
            "prepared": len(records),
            "errors": [],
            "signals": records,
        }


class FeishuHumanImportTests(unittest.TestCase):
    def test_first_video_and_comment_expand_to_independent_samples(self) -> None:
        records = {
            "human": [
                {
                    "record_id": "rec1",
                    "fields": {
                        "备注": "新版的肢体问题减少",
                        "旧版模型生成结果": [
                            {"file_token": "old-first", "name": "old.mp4"},
                            {"file_token": "old-second", "name": "old_02.mp4"},
                        ],
                        "新版模型生成结果": [
                            {"file_token": "new-first", "name": "new.mp4"},
                            {"file_token": "new-second", "name": "new_02.mp4"},
                        ],
                        "新版模型Prompt生成成功率": 1.0,
                    },
                },
                {
                    "record_id": "rec2",
                    "fields": {"备注": "", "旧版模型生成结果": []},
                },
            ]
        }
        client = FakeFeishuSyncClient(existing_records=records)
        media = CapturingMediaImporter()
        with tempfile.TemporaryDirectory() as directory:
            importer = FeishuHumanDatasetImporter(client, media, download_root=Path(directory))
            result = importer.import_source(
                {
                    "source_id": "human",
                    "app_token": "base",
                    "table_id": "human",
                    "comment_field": "备注",
                    "video_fields": ["旧版模型生成结果", "新版模型生成结果"],
                    "review_context": "unknown",
                }
            )
        self.assertEqual(result["candidate_samples"], 2)
        self.assertEqual(
            {item["attachment_token"] for item in media.records},
            {"old-first", "new-first"},
        )
        self.assertTrue(all(item["raw_text"] == "新版的肢体问题减少" for item in media.records))
        self.assertNotIn("新版模型Prompt生成成功率", media.records[0])


if __name__ == "__main__":
    unittest.main()
