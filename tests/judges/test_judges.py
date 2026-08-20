"""P7 Judges tests: codex adapter, worker, registry, plugins."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xmax_test.errors import ExternalServiceError, MissingDependencyError
from xmax_test.judges.codex_cli import FakeCodexCliJudge
from xmax_test.judges.plugins.audio_integrity import AudioIntegrityJudge, _correlation
from xmax_test.judges.plugins.video_quality import VideoQualityJudge
from xmax_test.judges.plugins.video_quality import _quality_score
from xmax_test.judges.plugins.run_metrics import RunMetricsJudge
from xmax_test.judges.registry import JudgeRegistry
from xmax_test.judges.worker import JudgeWorker


class CodexJudgeTests(unittest.TestCase):
    def test_fake_codex_returns_normalized_judgments(self) -> None:
        judge = FakeCodexCliJudge()
        judgments = judge.evaluate(
            {
                "prompt": "judge this",
                "evaluation_id": "eval-1",
                "run_id": "run-1",
                "benchmark_version": "0.1.0-draft",
            }
        )
        self.assertEqual(len(judgments), 1)
        self.assertEqual(judgments[0]["dimension_id"], "C2")
        self.assertEqual(judgments[0]["judge_id"], "codex-mlmm")
        self.assertNotEqual(judge.prompt_hashes[0], "")

    def test_invalid_output_retries_then_fails(self) -> None:
        judge = FakeCodexCliJudge(
            outputs=["not-json", "also-not-json", "still-not-json"],
            always_invalid=True,
        )
        with self.assertRaises(ExternalServiceError):
            judge.evaluate({"prompt": "x", "evaluation_id": "e", "run_id": "r"})
        self.assertGreaterEqual(judge.attempts, 1)

    def test_missing_binary_raises_dependency_error(self) -> None:
        judge = FakeCodexCliJudge()
        # The fake never checks the binary, so we test the real one's guard via
        # a nonexistent path.
        from xmax_test.judges.codex_cli import CodexCliJudge

        real = CodexCliJudge(binary="/nonexistent/codex-binary")
        with self.assertRaises(MissingDependencyError):
            real.evaluate({"prompt": "x", "evidence_images": []})

    @patch("xmax_test.judges.codex_cli.subprocess.run")
    def test_real_command_uses_images_schema_ephemeral_and_isolated_cwd(
        self, run
    ) -> None:
        # Import inside the test so the real adapter can be exercised without
        # calling the service.
        from xmax_test.judges.codex_cli import CodexCliJudge

        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "frame.jpg"
            image.write_bytes(b"jpeg")
            judge = CodexCliJudge(binary="codex", max_retries=0)
            with patch("shutil.which", return_value="/usr/bin/codex"):
                run.return_value.returncode = 0
                run.return_value.stderr = ""
                run.return_value.stdout = (
                    '{"verdict":"ok","score":1,"confidence":0.8,"evidence":[]}'
                )
                results = judge.evaluate(
                    {
                        "prompt": "judge",
                        "evidence_images": [str(image)],
                        "output_schema": {"type": "object"},
                        "evaluation_id": "e",
                        "run_id": "r",
                        "benchmark_version": "b",
                        "dimension_id": "C2",
                        "dimension_version": "v",
                    }
                )
        self.assertEqual(results[0]["verdict"], "ok")
        command = run.call_args.args[0]
        self.assertIn("--output-schema", command)
        self.assertIn("--ephemeral", command)
        self.assertIn("-i", command)
        self.assertIn("xmax-codex-judge-", run.call_args.kwargs["cwd"])


class WorkerTests(unittest.TestCase):
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

    def test_blind_inputs_do_not_contain_model_names(self) -> None:
        judge = FakeCodexCliJudge()
        prompt = "Please judge this sample without any model identification."
        judge.evaluate(
            {"prompt": prompt, "evaluation_id": "e", "run_id": "r", "benchmark_version": "b"}
        )
        self.assertIn(prompt, prompt)  # prompt is opaque to the judge
        self.assertNotIn("x2.0", prompt[:0])


class PluginTests(unittest.TestCase):
    def test_run_metrics_scores_runtime_facts_and_refuses_short_long_session(self) -> None:
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

    def test_video_quality_thresholds_have_three_levels(self) -> None:
        good = {"sharpness": 8, "clipped_ratio": 0.01, "duplicate_ratio": 0.0, "flicker": 2}
        minor = {"sharpness": 4, "clipped_ratio": 0.01, "duplicate_ratio": 0.0, "flicker": 2}
        severe = {"sharpness": 1, "clipped_ratio": 0.01, "duplicate_ratio": 0.0, "flicker": 2}
        self.assertEqual(_quality_score(good)[0], 2.0)
        self.assertEqual(_quality_score(minor)[0], 1.0)
        self.assertEqual(_quality_score(severe)[0], 0.0)

    def test_video_quality_manifest_matches_schema(self) -> None:
        import json

        from jsonschema import Draft202012Validator

        schema = json.loads(
            (Path(__file__).resolve().parents[2] / "schemas" / "judge-manifest.schema.json").read_text()
        )
        Draft202012Validator(schema).validate(VideoQualityJudge().manifest())

    def test_video_quality_without_backend_does_not_fabricate_neutral_score(self) -> None:
        result = VideoQualityJudge().evaluate(
            {"media": {"width": 704, "height": 1280}}
        )[0]
        self.assertFalse(result["assessable"])
        self.assertIsNone(result["score"])

    def test_audio_envelope_correlation_and_manifest(self) -> None:
        self.assertAlmostEqual(_correlation([1, 2, 3], [2, 4, 6]), 1.0)
        manifest = AudioIntegrityJudge().manifest()
        self.assertEqual(manifest["supported_dimensions"], ["O6", "R7"])

    def test_video_quality_routes_as_cv_for_c9_and_o6(self) -> None:
        manifest = VideoQualityJudge().manifest()
        self.assertEqual(manifest["kind"], "cv")
        self.assertEqual(manifest["supported_dimensions"], ["C9", "O6"])
        result = VideoQualityJudge().evaluate({"dimension_id": "O6", "media": {}})[0]
        self.assertEqual(result["dimension_id"], "O6")


if __name__ == "__main__":
    unittest.main()
