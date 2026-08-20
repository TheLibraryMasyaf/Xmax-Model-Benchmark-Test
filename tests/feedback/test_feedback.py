"""P9 Human Signals tests: importer, normalizer, router, proposals.

Acceptance: raw text immutable; low confidence never enters learning; unmapped
produces proposals; a single override never hot-updates a Judge; Holdout is
never readable by training routes.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator

from xmax_test.benchmark import load_benchmark_contract
from xmax_test.errors import ContractError
from xmax_test.feedback.importer import HumanSignalImporter
from xmax_test.feedback.normalizer import CodexNormalizer, RuleNormalizer
from xmax_test.feedback.proposals import DimensionProposalService
from xmax_test.feedback.router import LearningRouter
from xmax_test.judges.releases import JudgeReleaseService
from xmax_test.storage.sqlite import SqliteMetadataRepository

ROOT = Path(__file__).resolve().parents[2]


class FeedbackTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.repository = SqliteMetadataRepository(root / "db.sqlite3")
        self.benchmark = load_benchmark_contract(ROOT / "BENCHMARK.md")
        self.importer = HumanSignalImporter(self.repository)
        self.normalizer = RuleNormalizer(self.benchmark)
        self.router = LearningRouter(self.repository, self.benchmark)
        self.proposals = DimensionProposalService(self.repository)

    def tearDown(self) -> None:
        self.repository.close()
        self.directory.cleanup()

    def signal(self, raw_text: str, **extra: object) -> dict:
        record = {
            "signal_id": "signal-1",
            "sample_id": "feed001_prompt001_01",
            "raw_text": raw_text,
            "review_context": "blind",
            "source_type": "evaluation_feedback",
        }
        record.update(extra)
        return record


class ImporterTests(FeedbackTestBase):
    def test_raw_text_is_immutable(self) -> None:
        """Re-importing the same signal_id never overwrites the raw text."""
        outcome = self.importer.import_records([self.signal("换装效果很好")])
        self.assertEqual(outcome["imported"], 1)
        original = self.repository.get_human_signal("signal-1")
        # A different raw text with the same signal_id is ignored.
        self.importer.import_records([self.signal("不同文本，必须被忽略")])
        after = self.repository.get_human_signal("signal-1")
        self.assertEqual(after["raw_text"], original["raw_text"])
        self.assertEqual(after["raw_text"], "换装效果很好")

    def test_import_rejects_blank_raw_text(self) -> None:
        outcome = self.importer.import_records([self.signal("   ")])
        self.assertEqual(outcome["imported"], 0)
        self.assertTrue(outcome["errors"])

    def test_import_rejects_invalid_context(self) -> None:
        outcome = self.importer.import_records(
            [self.signal("ok", review_context="guessed")]
        )
        self.assertEqual(outcome["imported"], 0)
        self.assertTrue(outcome["errors"])

    def test_import_preserves_cv_supervision_and_asset_references(self) -> None:
        outcome = self.importer.import_records(
            [
                self.signal(
                    "手部在1.2秒到1.8秒扭曲",
                    annotations={"time_ranges_ms": [[1200, 1800]], "roi": [1, 2, 3, 4]},
                    feed_asset_id="asset-feed",
                    prompt_asset_ids=["asset-prompt"],
                    result_asset_id="asset-result",
                )
            ]
        )
        self.assertEqual(outcome["imported"], 1)
        signal = self.repository.get_human_signal("signal-1")
        self.assertEqual(signal["annotations"]["time_ranges_ms"], [[1200, 1800]])
        self.assertEqual(signal["result_asset_id"], "asset-result")


class NormalizerTests(FeedbackTestBase):
    @patch("xmax_test.feedback.normalizer.which", return_value="/usr/bin/codex")
    @patch("xmax_test.feedback.normalizer.subprocess.run")
    def test_codex_normalizer_cannot_rewrite_raw_text(self, run, _which) -> None:
        run.return_value.returncode = 0
        run.return_value.stderr = ""
        run.return_value.stdout = json.dumps(
            {
                "mapping_status": "existing",
                "normalized_labels": [
                    {"dimension_id": "C2", "confidence": 0.9, "rationale": "matched"}
                ],
                "raw_text": "malicious rewrite",
                "dimension_proposal": None,
            }
        )
        raw = self.signal("原始人工评价")
        normalized = CodexNormalizer().normalize(raw, self.benchmark)
        self.assertEqual(normalized["raw_text"], "原始人工评价")
        self.assertTrue(normalized["learning_permission"])

    def test_matched_keyword_maps_to_existing_dimension(self) -> None:
        raw = self.signal("编辑边界准确，换装效果自然")
        normalized = self.normalizer.normalize(raw, self.benchmark)
        self.assertEqual(normalized["mapping_status"], "existing")
        self.assertTrue(normalized["normalized_labels"])
        self.assertTrue(normalized["learning_permission"])

    def test_unmapped_produces_dimension_proposal(self) -> None:
        raw = self.signal("这是一段完全不相关的描述文本xyz")
        normalized = self.normalizer.normalize(raw, self.benchmark)
        self.assertEqual(normalized["mapping_status"], "unmapped")
        self.assertIsNotNone(normalized["dimension_proposal"])
        self.assertFalse(normalized["learning_permission"])

    def test_unmapped_proposal_lifecycle(self) -> None:
        raw = self.signal("全新的评测维度描述：肢体光影一致性")
        normalized = self.normalizer.normalize(raw, self.benchmark)
        proposal = self.proposals.propose(
            {
                "proposal_id": "prop-1",
                "name": normalized["dimension_proposal"]["name"],
                "definition": normalized["dimension_proposal"]["definition"],
            }
        )
        self.assertEqual(proposal["status"], "proposed")
        # draft -> shadow -> active, and illegal jumps are rejected.
        self.proposals.transition("prop-1", "draft")
        self.proposals.transition("prop-1", "shadow")
        self.proposals.transition("prop-1", "active")
        with self.assertRaises(ContractError):
            self.proposals.transition("prop-1", "draft")

    def test_low_confidence_never_enters_learning(self) -> None:
        """Unmapped (confidence 0) must be excluded from the learning pool."""
        raw = self.signal("无匹配关键词的文本")
        normalized = self.normalizer.normalize(raw, self.benchmark)
        self.assertFalse(normalized["learning_permission"])
        signal = {
            **normalized,
            "mapping_status": "unmapped",
            "learning_permission": False,
        }
        candidates = self.router.route(signal)
        self.assertEqual(candidates, [])


class RouterTests(FeedbackTestBase):
    def _existing_signal(self) -> dict:
        return {
            "signal_id": "signal-2",
            "sample_id": "feed001_prompt001_01",
            "raw_text": "编辑边界准确",
            "review_context": "blind",
            "mapping_status": "existing",
            "normalized_labels": [{"dimension_id": "C1", "confidence": 0.9}],
            "learning_permission": True,
            "data_partition": "train",
        }

    def test_holdout_is_never_readable_by_training(self) -> None:
        signal = self._existing_signal()
        signal["data_partition"] = "holdout"
        with self.assertRaises(ContractError):
            self.router.route(signal)

    def test_partition_assignment_is_deterministic(self) -> None:
        signal = self._existing_signal()
        first = self.router.assign_partition(signal)
        second = self.router.assign_partition(signal)
        self.assertEqual(first, second)

    def test_route_kind_by_dimension_family(self) -> None:
        signal = self._existing_signal()
        candidates = self.router.route(signal)
        self.assertTrue(candidates)
        self.assertEqual(candidates[0]["data_partition"], "train")

    def test_export_packet_contains_supervision_but_holdout_is_not_trainable(self) -> None:
        signal = {
            **self._existing_signal(),
            "data_partition": "holdout",
            "annotations": {"time_ranges_ms": [[100, 300]]},
            "result_asset_id": "asset-result",
        }
        packets = self.router.export_packets(signal)
        self.assertEqual(len(packets), 1)
        self.assertFalse(packets[0]["training_eligible"])
        self.assertEqual(
            packets[0]["supervision"]["annotations"]["time_ranges_ms"],
            [[100, 300]],
        )
        self.assertEqual(packets[0]["evidence_refs"]["result_asset_id"], "asset-result")
        schema = json.loads(
            (ROOT / "schemas" / "learning-candidate.schema.json").read_text(
                encoding="utf-8"
            )
        )
        Draft202012Validator(schema).validate(packets[0])


class OverrideTests(FeedbackTestBase):
    def test_single_override_does_not_hot_update_judge(self) -> None:
        """Recording one human signal never changes the judge champion state."""
        self.importer.import_records(
            [self.signal("这条人工反馈与模型输出不一致，请修正评分", sample_id="feed002")]
        )
        # No judge release was recorded as a side effect.
        self.assertIsNone(self.repository.get_judge_release("any-judge", "1.0.0"))
        # Promotion requires explicit validation evidence.
        service = JudgeReleaseService(self.repository)
        with self.assertRaises(ContractError):
            service.promote("video-quality", "1.0.0", validation={"valid": False})
        # Valid validation allows explicit promotion (not a hot update).
        outcome = service.promote("video-quality", "1.0.0", validation={"valid": True})
        self.assertEqual(outcome["status"], "champion")


if __name__ == "__main__":
    unittest.main()
