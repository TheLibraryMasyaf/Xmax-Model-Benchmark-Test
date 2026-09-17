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

from jsonschema import Draft202012Validator

from xmax_test.benchmark import load_benchmark_contract
from xmax_test.errors import ContractError
from xmax_test.feedback.importer import HumanSignalImporter
from xmax_test.feedback.normalizer import (
    MlmmHumanNormalizer,
    RuleNormalizer,
    canonicalize_normalized_signal,
)
from xmax_test.feedback.overrides import HumanOverrideService
from xmax_test.feedback.proposals import DimensionProposalService
from xmax_test.feedback.review import NormalizationReviewService
from xmax_test.feedback.router import LearningRouter
from xmax_test.feedback.training import HumanLearningService
from xmax_test.judges.mlmm.base import MlmmResponse
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
        outcome = self.importer.import_records([self.signal("ok", review_context="guessed")])
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
    def test_provider_normalizer_cannot_rewrite_raw_text(self) -> None:
        class Provider:
            provider_id = "fake"

            def complete_json(self, **_kwargs):
                payload = {
                    "mapping_status": "existing",
                    "normalized_labels": [
                        {
                            "dimension_id": "E2",
                            "confidence": 0.9,
                            "rationale": "matched",
                            "polarity": "positive",
                            "severity": "mild",
                        }
                    ],
                    "raw_text": "malicious rewrite",
                    "dimension_proposal": None,
                }
                return MlmmResponse(
                    payload=payload,
                    raw_text=json.dumps(payload),
                    provider_id=self.provider_id,
                    model="fake",
                )

        raw = self.signal("原始人工评价")
        normalized = MlmmHumanNormalizer(Provider()).normalize(raw, self.benchmark)
        self.assertEqual(normalized["raw_text"], "原始人工评价")
        self.assertFalse(normalized["learning_permission"])
        self.assertEqual(normalized["normalization_review"]["status"], "pending")

    def test_mlmm_mapping_requires_explicit_human_review_before_learning(self) -> None:
        class Provider:
            provider_id = "fake"

            def complete_json(self, **_kwargs):
                payload = {
                    "mapping_status": "existing",
                    "normalized_labels": [{
                        "dimension_id": "E2", "criterion_id": "E2.1", "score_hint": 1,
                        "confidence": 0.9, "rationale": "matched", "polarity": "negative",
                        "severity": "moderate",
                    }],
                    "dimension_proposal": None,
                }
                return MlmmResponse(payload=payload, raw_text=json.dumps(payload), provider_id="fake", model="fake")

        normalized = MlmmHumanNormalizer(Provider()).normalize(self.signal("边界破损"), self.benchmark)
        self.repository.append_human_signal(normalized)
        self.assertEqual(self.router.route(normalized), [])
        result = NormalizationReviewService(self.repository).apply([
            {"signal_id": "signal-1", "decision": "approved", "reviewer": "reviewer"}
        ])
        self.assertEqual(result["approved"], 1)
        reviewed = self.repository.get_human_signal("signal-1")
        self.assertTrue(reviewed["learning_permission"])

    def test_label_scoped_review_activates_only_confirmed_scored_criteria(self) -> None:
        signal = {
            **self.signal("边界较差，但人物还原准确"),
            "mapping_status": "existing",
            "normalizer_id": "mlmm-provider:qwen",
            "normalizer_version": "2.0.0",
            "learning_permission": False,
            "normalization_review": {"status": "pending"},
            "normalized_labels": [
                {
                    "dimension_id": "E2", "criterion_id": "E2.1", "score_hint": 0,
                    "confidence": 0.9, "polarity": "negative", "severity": "severe",
                },
                {
                    "dimension_id": "E3", "criterion_id": "E3.2", "score_hint": 2,
                    "confidence": 0.8, "polarity": "positive", "severity": "none",
                },
                {
                    "dimension_id": "E4", "criterion_id": None, "score_hint": None,
                    "confidence": 0.7, "polarity": "mixed", "severity": "unknown",
                },
            ],
        }
        self.repository.append_human_signal(signal)

        result = NormalizationReviewService(self.repository).apply([
            {
                "signal_id": "signal-1",
                "decision": "approved",
                "reviewer": "human",
                "approved_labels": [{"criterion_id": "E3.2", "score_hint": 2}],
                "review_source": {"type": "feishu_field_confirmation"},
            }
        ])

        self.assertEqual(result["approved"], 1)
        reviewed = self.repository.get_human_signal("signal-1")
        self.assertEqual(reviewed["normalization_review"]["scope"], "selected_labels")
        self.assertEqual(
            [item["criterion_id"] for item in reviewed["reviewed_normalized_labels"]],
            ["E3.2"],
        )
        packets = self.router.route({**reviewed, "data_partition": "train"})
        self.assertEqual(len(packets), 1)
        self.assertEqual(packets[0]["supervision"]["label"]["criterion_id"], "E3.2")
        self.assertIsNone(packets[0]["supervision"]["comparison"])

    def test_label_scoped_review_rejects_unknown_criterion(self) -> None:
        signal = {
            **self.signal("边界较差"),
            "mapping_status": "existing",
            "normalizer_id": "mlmm-provider:qwen",
            "normalizer_version": "2.0.0",
            "learning_permission": False,
            "normalization_review": {"status": "pending"},
            "normalized_labels": [{
                "dimension_id": "E2", "criterion_id": "E2.1", "score_hint": 0,
                "confidence": 0.9, "polarity": "negative", "severity": "severe",
            }],
        }
        self.repository.append_human_signal(signal)
        result = NormalizationReviewService(self.repository).apply([
            {
                "signal_id": "signal-1",
                "decision": "approved",
                "reviewer": "human",
                "approved_labels": [{"criterion_id": "E9.9", "score_hint": 2}],
            }
        ])
        self.assertEqual(result["approved"], 0)
        self.assertEqual(len(result["errors"]), 1)
        self.assertFalse(self.repository.get_human_signal("signal-1")["learning_permission"])

    def test_mlmm_normalizer_canonicalizes_criterion_id_used_as_dimension_id(self) -> None:
        class Provider:
            provider_id = "fake"

            def complete_json(self, **_kwargs):
                payload = {
                    "mapping_status": "existing",
                    "normalized_labels": [{
                        "dimension_id": "E2.1", "criterion_id": "E2.1", "score_hint": 0,
                        "confidence": 0.9, "rationale": "matched", "polarity": "negative",
                        "severity": "severe",
                    }],
                    "dimension_proposal": None,
                }
                return MlmmResponse(
                    payload=payload,
                    raw_text=json.dumps(payload),
                    provider_id="fake",
                    model="fake",
                )

        normalized = MlmmHumanNormalizer(Provider()).normalize(
            self.signal("编辑边界破损"), self.benchmark
        )
        self.assertEqual(normalized["normalized_labels"][0]["dimension_id"], "E2")
        self.assertEqual(normalized["normalized_labels"][0]["criterion_id"], "E2.1")

    def test_mlmm_normalizer_canonicalizes_invalid_optional_enums(self) -> None:
        class Provider:
            provider_id = "fake"

            def complete_json(self, **_kwargs):
                payload = {
                    "mapping_status": "existing",
                    "normalized_labels": [{
                        "dimension_id": "E2", "criterion_id": "E2.1", "score_hint": 9,
                        "confidence": 0.9, "rationale": "insufficient evidence",
                        "polarity": "unknown", "severity": "uncertain",
                    }],
                    "dimension_proposal": None,
                }
                return MlmmResponse(
                    payload=payload,
                    raw_text=json.dumps(payload),
                    provider_id="fake",
                    model="fake",
                )

        normalized = MlmmHumanNormalizer(Provider()).normalize(
            self.signal("证据不足"), self.benchmark
        )
        label = normalized["normalized_labels"][0]
        self.assertIsNone(label["score_hint"])
        self.assertEqual(label["polarity"], "neutral")
        self.assertEqual(label["severity"], "unknown")

    def test_existing_normalization_can_be_repaired_without_provider_call(self) -> None:
        signal = {
            **self.signal("证据不足"),
            "mapping_status": "existing",
            "normalizer_id": "mlmm-provider:qwen",
            "normalizer_version": "2.0.0",
            "normalized_labels": [{
                "dimension_id": "E3", "criterion_id": None, "score_hint": None,
                "confidence": 0.7, "rationale": "insufficient evidence",
                "polarity": "unknown", "severity": "unknown",
            }],
        }
        repaired = canonicalize_normalized_signal(signal, self.benchmark)
        self.assertEqual(repaired["normalized_labels"][0]["polarity"], "neutral")
        self.assertEqual(repaired["normalized_labels"][0]["severity"], "unknown")
        self.assertTrue(repaired["normalized_hash"])

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
            "normalized_labels": [{"dimension_id": "E2", "confidence": 0.9}],
            "signal_kind": "pairwise_projection",
            "comparison": {"relation": "better_than"},
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

    def test_export_packet_contains_supervision_but_holdout_is_not_trainable(
        self,
    ) -> None:
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
        self.assertEqual(
            packets[0]["supervision"]["comparison"]["relation"], "better_than"
        )
        self.assertEqual(
            packets[0]["supervision"]["signal_kind"], "pairwise_projection"
        )
        schema = json.loads(
            (ROOT / "schemas" / "learning-candidate.schema.json").read_text(encoding="utf-8")
        )
        Draft202012Validator(schema).validate(packets[0])


class OverrideTests(FeedbackTestBase):
    def test_mlmm_challenger_is_train_only_and_registered_as_shadow(self) -> None:
        train_path = Path(self.directory.name) / "learning.train.jsonl"
        packet = {
            "route_kind": "mlmm",
            "data_partition": "train",
            "training_eligible": True,
            "dimension_id": "C2",
            "signal_id": "signal-1",
            "supervision": {"raw_text": "边界错误", "label": {"polarity": "negative"}},
        }
        train_path.write_text(json.dumps(packet, ensure_ascii=False) + "\n", encoding="utf-8")
        result = HumanLearningService(self.repository).build_challenger(
            judge_id="mlmm-main",
            version="human-cal-1",
            route_kind="mlmm",
            train_path=train_path,
            output_directory=Path(self.directory.name) / "challengers",
        )
        self.assertEqual(result["status"], "shadow")
        self.assertTrue(Path(result["training"]["artifact_uri"]).is_file())
        self.assertEqual(
            self.repository.get_judge_release("mlmm-main", "human-cal-1")["status"],
            "shadow",
        )

    def test_human_override_is_idempotent_and_original_ai_result_is_preserved(
        self,
    ) -> None:
        evaluation = {
            "evaluation_id": "eval-human",
            "evaluation_batch_id": "eval-batch",
            "run_id": "run-human",
            "benchmark_version": "0.2.0-draft",
            "scenario_pack_version": "0.1.0-draft",
            "score_schema_version": "2.0.0",
            "criterion_results": [],
            "dimension_results": [],
            "weight_resolution": {},
            "case_score_percent": 40.0,
            "canonical_score": 40.0,
            "scenario_score": 40.0,
            "applied_gate_ids": [],
            "final_verdict": None,
        }
        self.repository.save_evaluation_result(evaluation)
        signal = {
            "signal_id": "human-score-1",
            "sample_id": "sample-human",
            "source_type": "evaluation_feedback",
            "review_context": "ai_assisted",
            "evaluation_id": "eval-human",
            "human_score_percent": 75.0,
            "raw_text": "人工评为75%",
            "annotations": {},
        }
        self.repository.append_human_signal(signal)
        service = HumanOverrideService(self.repository)
        first = service.apply_signal(signal)
        second = service.apply_signal(signal)
        self.assertEqual(first["override_id"], second["override_id"])
        self.assertTrue(second["reused"])
        self.assertEqual(service.effective_result(evaluation)["effective_case_score_percent"], 75.0)
        self.assertEqual(
            self.repository.get_evaluation_result("eval-human")["case_score_percent"],
            40.0,
        )

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
        # Valid Holdout evidence can promote an explicitly built shadow.
        self.repository.record_judge_release(
            "video-quality",
            "1.0.0",
            "shadow",
            {"judge_id": "video-quality", "version": "1.0.0", "status": "shadow"},
        )
        outcome = service.promote(
            "video-quality",
            "1.0.0",
            validation={"valid": True, "data_partition": "holdout"},
        )
        self.assertEqual(outcome["status"], "champion")

    def test_criterion_override_recomputes_scores_without_mutating_ai(self) -> None:
        criteria = [
            {
                "dimension_id": "C2",
                "criterion_id": "C2.1",
                "score": 0.0,
                "assessable": True,
                "applicable": True,
            },
            {
                "dimension_id": "C2",
                "criterion_id": "C2.2",
                "score": 2.0,
                "assessable": True,
                "applicable": True,
            },
        ]
        evaluation = {
            "evaluation_id": "eval-criteria",
            "evaluation_batch_id": "eval-batch",
            "run_id": "run-criteria",
            "benchmark_version": "0.2.0-draft",
            "criterion_results": criteria,
            "dimension_results": [
                {
                    "dimension_id": "C2",
                    "score": 1.0,
                    "criterion_results": criteria,
                }
            ],
            "weight_resolution": {
                "effective_weights": {"C2": 1.0},
                "canonical_weights": {"C2": 1.0},
            },
            "case_score_percent": 50.0,
            "canonical_score": 50.0,
            "scenario_score": 50.0,
        }
        self.repository.save_evaluation_result(evaluation)
        signal = {
            "signal_id": "human-criteria-1",
            "evaluation_id": "eval-criteria",
            "annotations": {"criterion_scores": {"C2.1": 2.0}},
            "raw_text": "C2.1应为2分",
        }
        self.repository.append_human_signal(signal)
        service = HumanOverrideService(self.repository)
        service.apply_signal(signal)

        effective = service.effective_result(evaluation)
        self.assertEqual(effective["effective_case_score_percent"], 100.0)
        self.assertEqual(effective["canonical_score"], 100.0)
        self.assertEqual(effective["dimension_results"][0]["score"], 2.0)
        self.assertEqual(
            self.repository.get_evaluation_result("eval-criteria")["case_score_percent"],
            50.0,
        )

        typo = {
            "signal_id": "human-criteria-typo",
            "evaluation_id": "eval-criteria",
            "annotations": {"criterion_scores": {"C2.99": 2.0}},
        }
        with self.assertRaises(ContractError):
            service.apply_signal(typo)


if __name__ == "__main__":
    unittest.main()
