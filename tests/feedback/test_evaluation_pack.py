from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from jsonschema import Draft202012Validator

from xmax_test.feedback.evaluation_pack import HumanEvaluationPackImporter
from xmax_test.feedback.preprocessors.xlive_pairwise import build_pack
from xmax_test.errors import ContractError

ROOT = Path(__file__).resolve().parents[2]


class FakeClient:
    def __init__(self) -> None:
        self.downloads = 0

    def get_fields(self, _app: str, table: str):
        names = (
            [
                "测试结果编号", "记录用途", "评价人", "创建时间", "评价编号", "关联样本", "对比评价",
                "Xmax评语", "Decart评语",
            ]
            if table == "reviews"
            else [
                "测试结果编号", "核心场景", "prompt文字", "feed视频", "prompt素材",
                "Xmax视频", "Decart视频",
            ]
        )
        return [{"id": f"fld-{index}", "name": name} for index, name in enumerate(names)]

    def get_bitable_records(self, _app: str, table: str, page_token=None):
        del page_token
        if table == "reviews":
            return {
                "records": [
                    self._review("old", 1, "旧评语", "旧评语", "Xmax"),
                    self._review("latest", 2, "人物完整", "边界破损", "Xmax"),
                    {
                        **self._review("other", 3, "x", "y", "Decart"),
                        "fields": {
                            **self._review("other", 3, "x", "y", "Decart")["fields"],
                            "评价人": [{"id": "someone-else"}],
                        },
                    },
                ],
                "has_more": False,
            }
        return {
            "records": [
                {
                    "record_id": "sample-1",
                    "fields": {
                        "测试结果编号": "feed007_prompt003",
                        "核心场景": "室内单人",
                        "prompt文字": "replace",
                        "feed视频": [{"file_token": "feed", "name": "feed.mp4"}],
                        "prompt素材": [{"file_token": "prompt", "name": "p.png"}],
                        "Xmax视频": [{"file_token": "x-token", "name": "x.mp4"}],
                        "Decart视频": [{"file_token": "d-token", "name": "d.mp4"}],
                    },
                }
            ],
            "has_more": False,
        }

    def _review(self, record_id, created_at, xmax_comment, decart_comment, preference):
        return {
            "record_id": record_id,
            "fields": {
                "测试结果编号": "feed007_prompt003",
                "记录用途": "人工评价",
                "评价人": [{"id": "reviewer"}],
                "创建时间": created_at,
                "评价编号": created_at,
                "关联样本": [{"id": "sample-1"}],
                "对比评价": preference,
                "Xmax评语": xmax_comment,
                "Decart评语": decart_comment,
            },
        }

    def download_attachment(self, _token, destination, **_kwargs):
        self.downloads += 1
        Path(destination).write_bytes(b"video")
        return {"path": str(destination)}


class CapturingMediaImporter:
    def __init__(self) -> None:
        self.records = []

    def import_records(self, records, *, source_file):
        self.records = records
        return {
            "imported": len(records),
            "errors": [],
            "signals": records,
            "source_file": source_file,
        }


def config():
    return {
        "source_id": "source",
        "app_token": "base",
        "review_context": "blind",
        "options": {
            "review_table_id": "reviews",
            "sample_table_id": "samples",
            "reviewer_user_id": "reviewer",
            "purpose_value": "人工评价",
            "review_fields": {
                "test_result_id": "测试结果编号", "purpose": "记录用途", "reviewer": "评价人",
                "created_at": "创建时间", "evaluation_number": "评价编号", "sample_link": "关联样本",
                "preference": "对比评价",
            },
            "sample_fields": {
                "test_result_id": "测试结果编号", "scenario": "核心场景", "prompt_text": "prompt文字",
                "feed_video": "feed视频", "prompt_asset": "prompt素材",
            },
            "sides": {
                "xmax": {"video_field": "Xmax视频", "comment_field": "Xmax评语", "model_version": "x2.0", "preference_value": "Xmax"},
                "decart": {"video_field": "Decart视频", "comment_field": "Decart评语", "model_version": "Lucy", "preference_value": "Decart"},
            },
        },
    }


class EvaluationPackTests(unittest.TestCase):
    def test_latest_filtered_review_becomes_canonical_pair_and_two_projections(self):
        client = FakeClient()
        pack = build_pack(client, config())
        schema = __import__("json").loads(
            (ROOT / "schemas" / "human-evaluation-import-pack.schema.json").read_text(encoding="utf-8")
        )
        Draft202012Validator(schema).validate(pack)
        self.assertEqual(pack["audit"]["eligible_review_records"], 2)
        self.assertEqual(pack["audit"]["materialized_comparisons"], 1)
        self.assertEqual(pack["evaluations"][0]["source_record_id"], "latest")

        with tempfile.TemporaryDirectory() as directory:
            media = CapturingMediaImporter()
            outcome = HumanEvaluationPackImporter(
                client, media, download_root=directory
            ).import_pack(pack, source_file="pack.json")
        self.assertEqual(outcome["candidate_projections"], 2)
        relations = {record["comparison"]["relation"] for record in media.records}
        self.assertEqual(relations, {"better_than", "worse_than"})
        self.assertTrue(all(record["source_group_id"] == "feed007" for record in media.records))
        self.assertEqual(client.downloads, 2)

    def test_import_rejects_tampered_field_snapshot(self):
        pack = build_pack(FakeClient(), config())
        tampered = deepcopy(pack)
        tampered["evaluations"][0]["participants"][0]["comment"]["value"] = "tampered"
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ContractError):
                HumanEvaluationPackImporter(
                    FakeClient(), CapturingMediaImporter(), download_root=directory
                ).import_pack(tampered)


if __name__ == "__main__":
    unittest.main()
