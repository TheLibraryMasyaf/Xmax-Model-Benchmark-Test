from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from xmax_test.errors import EvaluationInfrastructurePausedError, MlmmTimeoutError
from xmax_test.feedback.normalizer import MlmmHumanNormalizer
from xmax_test.judges.mlmm.base import MlmmResponse
from xmax_test.judges.mlmm.judge import MlmmJudge, _batch_output_schema
from xmax_test.judges.registry import JudgeRegistry
from xmax_test.judges.worker import JudgeWorker
from xmax_test.storage.artifacts import ArtifactStore


class FakeProvider:
    def __init__(self) -> None:
        self.calls = []

    @property
    def provider_id(self) -> str:
        return "fake_mlmm"

    def complete_json(self, *, prompt, image_paths, output_schema, media_inputs=None):
        self.calls.append(
            {
                "prompt": prompt,
                "image_paths": image_paths,
                "media_inputs": media_inputs,
                "schema": output_schema,
            }
        )
        if "judgments" in output_schema.get("properties", {}):
            branches = output_schema["properties"]["judgments"]["items"]["oneOf"]
            payload = {
                "judgments": [
                    {
                        "dimension_id": branch["properties"]["dimension_id"]["const"],
                        "verdict": "ok",
                        "confidence": 0.8,
                        "assessable": True,
                        "evidence": [{"description": "visible dimension"}],
                        "criterion_results": [
                            {
                                "criterion_id": criterion_id,
                                "verdict": "ok",
                                "score": 1.0,
                                "confidence": 0.8,
                                "assessable": True,
                                "evidence": [{"description": f"visible {criterion_id}"}],
                            }
                            for criterion_id in branch["properties"]["criterion_results"]["items"][
                                "properties"
                            ]["criterion_id"]["enum"]
                        ],
                    }
                    for branch in branches
                ]
            }
        else:
            payload = {
                "mapping_status": "existing",
                "normalized_labels": [
                    {
                        "dimension_id": "C7",
                        "confidence": 0.9,
                        "rationale": "visible extra limb",
                        "polarity": "negative",
                        "severity": "moderate",
                    }
                ],
                "dimension_proposal": None,
            }
        return MlmmResponse(
            payload=payload,
            raw_text=json.dumps(payload),
            provider_id=self.provider_id,
            model="fake-model",
            usage={"input_tokens": 10},
        )


class MlmmBatchTests(unittest.TestCase):
    def test_batch_schema_uses_natural_dimension_order(self) -> None:
        schema = _batch_output_schema(
            {"C10": ["C10.1"], "C2": ["C2.1"], "O1": ["O1.1"]}
        )
        branches = schema["properties"]["judgments"]["items"]["oneOf"]
        self.assertEqual(
            [branch["properties"]["dimension_id"]["const"] for branch in branches],
            ["C2", "C10", "O1"],
        )

    def test_one_provider_call_returns_multiple_dimension_judgments(self) -> None:
        provider = FakeProvider()
        judge = MlmmJudge(
            provider,
            judge_id="mlmm",
            version="2",
            supported_dimensions=["C2", "C10"],
            supported_modes=["offline"],
        )
        registry = JudgeRegistry()
        registry.register(judge)
        worker = JudgeWorker(registry)
        results = worker.run_batch(
            evaluation_id="e",
            run_id="r",
            benchmark_version="b",
            mode="offline",
            context={
                "prompt": "blind batch",
                "dimension_contracts": [
                    {
                        "dimension_id": "C2",
                        "version": "v1",
                        "criteria": [{"criterion_id": "C2.1"}],
                    },
                    {
                        "dimension_id": "C10",
                        "version": "v1",
                        "criteria": [{"criterion_id": "C10.1"}],
                    },
                ],
                "evidence_images": [],
                "media_inputs": [
                    {"role": "feed", "kind": "video", "path": "feed.mp4"},
                    {"role": "prompt_text", "kind": "text", "text": "edit"},
                    {"role": "result_video", "kind": "video", "path": "result.mp4"},
                ],
            },
            judge_id="mlmm",
            judge_version="2",
            dimension_versions={"C2": "v1", "C10": "v1"},
        )
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual({item["dimension_id"] for item in results}, {"C2", "C10"})
        self.assertEqual(
            [item["role"] for item in provider.calls[0]["media_inputs"]],
            ["feed", "prompt_text", "result_video"],
        )

    def test_batch_provider_failure_is_atomic_not_partial_judge_errors(self) -> None:
        class FailingProvider(FakeProvider):
            def complete_json(self, **kwargs):
                raise RuntimeError("quota exhausted")

        judge = MlmmJudge(
            FailingProvider(),
            judge_id="mlmm",
            version="2",
            supported_dimensions=["C2", "C10"],
            supported_modes=["offline"],
            max_retries=0,
        )
        registry = JudgeRegistry()
        registry.register(judge)
        worker = JudgeWorker(registry)

        with self.assertRaisesRegex(Exception, "no valid result"):
            worker.run_batch(
                evaluation_id="e",
                run_id="r",
                benchmark_version="b",
                mode="offline",
                context={
                    "prompt": "blind batch",
                    "dimension_contracts": [
                        {
                            "dimension_id": "C2",
                            "version": "v1",
                            "criteria": [{"criterion_id": "C2.1"}],
                        },
                        {
                            "dimension_id": "C10",
                            "version": "v1",
                            "criteria": [{"criterion_id": "C10.1"}],
                        },
                    ],
                },
                judge_id="mlmm",
                judge_version="2",
                dimension_versions={"C2": "v1", "C10": "v1"},
            )

    def test_transport_timeout_is_not_retried_by_judge(self) -> None:
        class TimeoutProvider(FakeProvider):
            def complete_json(self, **kwargs):
                self.calls.append(kwargs)
                raise MlmmTimeoutError("bounded timeout")

        provider = TimeoutProvider()
        judge = MlmmJudge(
            provider,
            judge_id="mlmm",
            version="2",
            supported_dimensions=["C2"],
            supported_modes=["offline"],
            max_retries=5,
        )
        with self.assertRaises(MlmmTimeoutError):
            judge.evaluate(
                {
                    "run_id": "run-timeout",
                    "prompt": "blind batch",
                    "dimension_contracts": [
                        {
                            "dimension_id": "C2",
                            "version": "v1",
                            "criteria": [{"criterion_id": "C2.1"}],
                        }
                    ],
                }
            )
        self.assertEqual(len(provider.calls), 1)

    def test_paid_schema_failure_is_not_retried_by_judge(self) -> None:
        class PaidInvalidProvider(FakeProvider):
            def complete_json(self, **kwargs):
                self.calls.append(kwargs)
                payload = {"judgments": []}
                return MlmmResponse(
                    payload=payload,
                    raw_text=json.dumps(payload),
                    provider_id=self.provider_id,
                    model="paid-model",
                    usage={"input_tokens": 10},
                    metadata={"paid_fallback": True},
                )

        provider = PaidInvalidProvider()
        with tempfile.TemporaryDirectory() as temporary:
            artifacts = ArtifactStore(Path(temporary) / "artifacts")
            judge = MlmmJudge(
                provider,
                judge_id="mlmm",
                version="2",
                supported_dimensions=["C2"],
                supported_modes=["offline"],
                max_retries=5,
                artifact_store=artifacts,
            )
            with self.assertRaises(EvaluationInfrastructurePausedError):
                judge.evaluate(
                    {
                        "run_id": "run-paid-invalid",
                        "prompt": "blind batch",
                        "dimension_contracts": [
                            {
                                "dimension_id": "C2",
                                "version": "v1",
                                "criteria": [{"criterion_id": "C2.1"}],
                            }
                        ],
                    }
                )
            raw = artifacts.list("evaluations")
            self.assertEqual(len(raw), 1)
            saved = json.loads(artifacts.read_bytes(raw[0]).decode("utf-8"))
            self.assertEqual(json.loads(saved["raw_text"]), {"judgments": []})
        self.assertEqual(len(provider.calls), 1)

    def test_only_anonymous_train_human_anchors_enter_prompt(self) -> None:
        provider = FakeProvider()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "learning.train.jsonl"
            packets = [
                {
                    "signal_id": "secret-train-id",
                    "dimension_id": "C2",
                    "data_partition": "train",
                    "training_eligible": True,
                    "supervision": {
                        "raw_text": "人物风格稳定",
                        "comparison": {"relation": "better_than"},
                        "label": {
                            "polarity": "positive",
                            "severity": "none",
                            "rationale": "多帧一致",
                        },
                    },
                },
                {
                    "signal_id": "secret-holdout-id",
                    "dimension_id": "C2",
                    "data_partition": "holdout",
                    "training_eligible": False,
                    "supervision": {
                        "raw_text": "留出集不可见",
                        "label": {"polarity": "negative"},
                    },
                },
            ]
            path.write_text(
                "\n".join(json.dumps(item, ensure_ascii=False) for item in packets),
                encoding="utf-8",
            )
            judge = MlmmJudge(
                provider,
                judge_id="mlmm",
                version="2",
                supported_dimensions=["C2"],
                supported_modes=["offline"],
                calibration_path=path,
            )
            judge.evaluate(
                {
                    "prompt": "blind batch",
                    "dimension_contracts": [
                        {
                            "dimension_id": "C2",
                            "version": "v1",
                            "criteria": [{"criterion_id": "C2.1"}],
                        }
                    ],
                    "evidence_images": [],
                }
            )
        sent = provider.calls[0]["prompt"]
        self.assertIn("人物风格稳定", sent)
        self.assertIn('"pairwise_relation":"better_than"', sent)
        self.assertIn('"anchor_type":"qualitative"', sent)
        self.assertNotIn("留出集不可见", sent)
        self.assertNotIn("secret-train-id", sent)

    def test_scored_anchor_is_preferred_over_qualitative_anchor(self) -> None:
        provider = FakeProvider()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "learning.train.jsonl"
            packets = [
                {
                    "signal_id": "qualitative-first",
                    "dimension_id": "C2",
                    "confidence": 0.99,
                    "data_partition": "train",
                    "training_eligible": True,
                    "supervision": {
                        "raw_text": "只有方向的定性锚点",
                        "label": {"criterion_id": None, "score_hint": None},
                    },
                },
                {
                    "signal_id": "scored-second",
                    "dimension_id": "C2",
                    "confidence": 0.8,
                    "data_partition": "train",
                    "training_eligible": True,
                    "supervision": {
                        "raw_text": "有明确细则分值的锚点",
                        "label": {"criterion_id": "C2.1", "score_hint": 2},
                    },
                },
            ]
            path.write_text(
                "\n".join(json.dumps(item, ensure_ascii=False) for item in packets),
                encoding="utf-8",
            )
            judge = MlmmJudge(
                provider,
                judge_id="mlmm",
                version="2",
                supported_dimensions=["C2"],
                supported_modes=["offline"],
                calibration_path=path,
                max_examples_per_dimension=1,
            )
            judge.evaluate(
                {
                    "prompt": "blind batch",
                    "dimension_contracts": [
                        {
                            "dimension_id": "C2",
                            "version": "v1",
                            "criteria": [{"criterion_id": "C2.1"}],
                        }
                    ],
                    "evidence_images": [],
                }
            )
        sent = provider.calls[0]["prompt"]
        self.assertIn("有明确细则分值的锚点", sent)
        self.assertNotIn("只有方向的定性锚点", sent)

    def test_human_normalizer_uses_video_evidence_and_provider(self) -> None:
        provider = FakeProvider()
        signal = {
            "signal_id": "s1",
            "raw_text": "人物多出一只手",
            "review_context": "blind",
            "evidence_images": ["frame.jpg"],
        }
        benchmark = {
            "dimensions": [
                {
                    "dimension_id": "C7",
                    "name": "structure",
                    "definition": "limbs",
                    "criteria": [],
                }
            ]
        }
        result = MlmmHumanNormalizer(provider).normalize(signal, benchmark)
        self.assertFalse(result["learning_permission"])
        self.assertEqual(result["normalization_review"]["status"], "pending")
        self.assertEqual(result["normalizer_id"], "mlmm-provider:fake_mlmm")
        self.assertEqual(provider.calls[0]["image_paths"], ["frame.jpg"])


if __name__ == "__main__":
    unittest.main()
