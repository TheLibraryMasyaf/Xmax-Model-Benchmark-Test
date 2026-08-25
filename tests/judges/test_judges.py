"""P7 Judges tests: codex adapter, worker, registry, plugins."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xmax_test.errors import MissingDependencyError, ValidationError
from xmax_test.judges.mlmm.codex import CodexCliProvider
from xmax_test.judges.plugins.audio_integrity import AudioIntegrityJudge, _correlation
from xmax_test.judges.plugins.run_metrics import RunMetricsJudge
from xmax_test.judges.plugins.video_quality import VideoQualityJudge, _quality_score
from xmax_test.judges.registry import JudgeRegistry
from xmax_test.judges.worker import JudgeWorker


class CodexProviderTests(unittest.TestCase):
    def test_missing_binary_raises_dependency_error(self) -> None:
        real = CodexCliProvider(binary="/nonexistent/codex-binary")
        with self.assertRaises(MissingDependencyError):
            real.complete_json(prompt="x", image_paths=[], output_schema={})

    @patch("xmax_test.judges.mlmm.codex.subprocess.run")
    def test_real_command_uses_images_schema_ephemeral_and_isolated_cwd(self, run) -> None:
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "frame.jpg"
            image.write_bytes(b"jpeg")
            provider = CodexCliProvider(binary="codex")
            with patch("xmax_test.judges.mlmm.codex.which", return_value="/usr/bin/codex"):
                run.return_value.returncode = 0
                run.return_value.stderr = ""
                run.return_value.stdout = '{"verdict":"ok"}'
                response = provider.complete_json(
                    prompt="judge",
                    image_paths=[str(image)],
                    output_schema={"type": "object"},
                )
        self.assertEqual(response.payload["verdict"], "ok")
        command = run.call_args.args[0]
        self.assertIn("--output-schema", command)
        self.assertIn("--ephemeral", command)
        self.assertIn("-i", command)
        self.assertIn("xmax-mlmm-codex-", run.call_args.kwargs["cwd"])


class WorkerTests(unittest.TestCase):
    def test_worker_rejects_dimension_only_score(self) -> None:
        class LegacyJudge:
            def manifest(self):
                return {
                    "judge_id": "legacy",
                    "version": "1",
                    "kind": "metric",
                    "supported_dimensions": ["C1"],
                    "supported_modes": ["offline"],
                }

            def evaluate(self, context):
                return [
                    {
                        "dimension_id": "C1",
                        "verdict": "legacy",
                        "score": 2.0,
                        "confidence": 1.0,
                        "assessable": True,
                        "evidence": [],
                    }
                ]

        registry = JudgeRegistry()
        registry.register(LegacyJudge())
        with self.assertRaisesRegex(ValidationError, "criterion_results"):
            JudgeWorker(registry).run(
                evaluation_id="e",
                run_id="r",
                benchmark_version="b",
                dimension_id="C1",
                dimension_version="v",
                mode="offline",
                context={"dimension_contract": {"criteria": [{"criterion_id": "C1.1"}]}},
            )

    def test_worker_routes_dimension_and_validates(self) -> None:
        registry = JudgeRegistry()
        registry.register(VideoQualityJudge())
        worker = JudgeWorker(registry)
        results = worker.run(
            evaluation_id="eval-1",
            run_id="run-1",
            benchmark_version="0.1.0-draft",
            dimension_id="C9",
            dimension_version="0.1.0-draft",
            mode="offline",
            context={"media": {"width": 704, "height": 1280}},
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["judge_id"], "video-quality-cv")
        self.assertIn("evidence", results[0])

    def test_no_judge_returns_no_automated_judge(self) -> None:
        registry = JudgeRegistry()
        worker = JudgeWorker(registry)
        results = worker.run(
            evaluation_id="eval-1",
            run_id="run-1",
            benchmark_version="b",
            dimension_id="C2",
            dimension_version="v",
            mode="offline",
            context={},
        )
        self.assertEqual(results[0]["status"], "no_automated_judge")

    def test_judge_error_is_recorded_not_fatal(self) -> None:
        class BrokenJudge:
            def manifest(self):
                return {
                    "judge_id": "broken",
                    "version": "1",
                    "supported_dimensions": ["C9"],
                    "supported_modes": ["offline"],
                }

            def evaluate(self, context):
                raise RuntimeError("backend down")

        registry = JudgeRegistry()
        registry.register(BrokenJudge())
        worker = JudgeWorker(registry)
        results = worker.run(
            evaluation_id="e",
            run_id="r",
            benchmark_version="b",
            dimension_id="C9",
            dimension_version="v",
            mode="offline",
            context={},
        )
        self.assertEqual(results[0]["verdict"], "judge_error")
        self.assertIn("error", results[0])


class PluginTests(unittest.TestCase):
    def test_run_metrics_scores_runtime_facts_and_refuses_short_long_session(
        self,
    ) -> None:
        judge = RunMetricsJudge()
        c1 = judge.evaluate(
            {
                "dimension_id": "C1",
                "run": {
                    "status": "completed",
                    "result_asset_id": "asset-result",
                    "metrics": {},
                },
            }
        )[0]
        self.assertEqual(c1["score"], 2.0)
        r6 = judge.evaluate(
            {
                "dimension_id": "R6",
                "run": {"metrics": {"session_duration_s": 3, "fps_window_cv": 0}},
            }
        )[0]
        self.assertFalse(r6["assessable"])
        self.assertFalse(r6["criterion_results"][0]["applicable"])

    def test_video_quality_thresholds_have_three_levels(self) -> None:
        good = {
            "sharpness": 8,
            "clipped_ratio": 0.01,
            "duplicate_ratio": 0.0,
            "flicker": 2,
        }
        minor = {
            "sharpness": 4,
            "clipped_ratio": 0.01,
            "duplicate_ratio": 0.0,
            "flicker": 2,
        }
        severe = {
            "sharpness": 1,
            "clipped_ratio": 0.01,
            "duplicate_ratio": 0.0,
            "flicker": 2,
        }
        self.assertEqual(_quality_score(good)[0], 2.0)
        self.assertEqual(_quality_score(minor)[0], 1.0)
        self.assertEqual(_quality_score(severe)[0], 0.0)

    def test_video_quality_manifest_matches_schema(self) -> None:
        import json

        from jsonschema import Draft202012Validator

        schema = json.loads(
            (
                Path(__file__).resolve().parents[2] / "schemas" / "judge-manifest.schema.json"
            ).read_text()
        )
        Draft202012Validator(schema).validate(VideoQualityJudge().manifest())

    def test_video_quality_without_backend_does_not_fabricate_neutral_score(
        self,
    ) -> None:
        result = VideoQualityJudge().evaluate({"media": {"width": 704, "height": 1280}})[0]
        self.assertFalse(result["assessable"])
        self.assertIsNone(result["score"])

    def test_audio_envelope_correlation_and_manifest(self) -> None:
        self.assertAlmostEqual(_correlation([1, 2, 3], [2, 4, 6]), 1.0)
        manifest = AudioIntegrityJudge().manifest()
        self.assertEqual(manifest["supported_dimensions"], ["O6", "R7"])

    def test_static_touch_capture_has_no_audio_preservation_requirement(self) -> None:
        result = AudioIntegrityJudge().evaluate(
            {
                "dimension_id": "R7",
                "test_case": {
                    "api_asset_bindings": {"input_media_role": "feed_capture"}
                },
            }
        )[0]
        self.assertFalse(result["assessable"])
        self.assertIsNone(result["score"])
        self.assertIn("static Feed capture", result["evidence"][0]["description"])

    def test_video_quality_routes_as_cv_for_c9_and_o6(self) -> None:
        manifest = VideoQualityJudge().manifest()
        self.assertEqual(manifest["kind"], "cv")
        self.assertEqual(manifest["supported_dimensions"], ["C9", "O6"])
        result = VideoQualityJudge().evaluate({"dimension_id": "O6", "media": {}})[0]
        self.assertEqual(result["dimension_id"], "O6")


if __name__ == "__main__":
    unittest.main()
