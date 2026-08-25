"""Realtime Feed capture policy and browser-input tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from xmax_test.errors import ContractError, ExternalServiceError
from xmax_test.generation.realtime.captures import (
    SEEDED_RANDOM_SAFE_WINDOW_V1,
    SeededFrameCaptureExtractor,
)
from xmax_test.generation.realtime.harness import BrowserRealtimeHarness
from xmax_test.storage.artifacts import ArtifactStore


class SeededFrameCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.artifacts = ArtifactStore(self.root / "artifacts")
        self.source = self.root / "feed.mp4"
        self.source.write_bytes(b"fake-video")
        self.asset = {
            "asset_id": "feed-a",
            "kind": "feed_video",
            "sha256": "sha-feed-a",
            "media": {"duration_s": 20.0},
        }
        self.extractor = SeededFrameCaptureExtractor(self.artifacts)

    def tearDown(self) -> None:
        self.directory.cleanup()

    @staticmethod
    def _successful_ffmpeg(command, **_kwargs):
        Path(command[-1]).write_bytes(b"\xff\xd8\xffcapture")
        return SimpleNamespace(returncode=0, stderr="")

    @patch("xmax_test.generation.realtime.captures.shutil.which", return_value="ffmpeg")
    @patch("xmax_test.generation.realtime.captures.subprocess.run")
    def test_capture_is_random_per_case_reproducible_and_cached(self, run, _which) -> None:
        run.side_effect = self._successful_ffmpeg
        first = self.extractor.extract(
            case={"case_id": "case-a"},
            source_asset=self.asset,
            source_path=self.source,
            policy=SEEDED_RANDOM_SAFE_WINDOW_V1,
        )
        repeated = self.extractor.extract(
            case={"case_id": "case-a"},
            source_asset=self.asset,
            source_path=self.source,
            policy=SEEDED_RANDOM_SAFE_WINDOW_V1,
        )
        different = self.extractor.extract(
            case={"case_id": "case-b"},
            source_asset=self.asset,
            source_path=self.source,
            policy=SEEDED_RANDOM_SAFE_WINDOW_V1,
        )

        self.assertEqual(first, repeated)
        self.assertNotEqual(first["uri"], different["uri"])
        self.assertNotEqual(first["timestamp_s"], different["timestamp_s"])
        self.assertTrue(2.0 <= first["timestamp_s"] <= 18.0)
        self.assertEqual(run.call_count, 2)
        self.assertTrue(self.artifacts.resolve(first["uri"]).is_file())

    def test_invalid_capture_duration_fails_before_external_call(self) -> None:
        with self.assertRaises(ContractError):
            self.extractor.select_timestamp(
                case_id="case-a",
                source_sha256="sha",
                duration_s=0,
                policy=SEEDED_RANDOM_SAFE_WINDOW_V1,
            )

    @patch("xmax_test.generation.realtime.captures.shutil.which", return_value="ffmpeg")
    @patch("xmax_test.generation.realtime.captures.subprocess.run")
    def test_ffmpeg_failure_is_not_treated_as_a_valid_capture(self, run, _which) -> None:
        run.return_value = SimpleNamespace(returncode=1, stderr="decode failed")
        with self.assertRaises(ExternalServiceError):
            self.extractor.extract(
                case={"case_id": "case-a"},
                source_asset=self.asset,
                source_path=self.source,
                policy=SEEDED_RANDOM_SAFE_WINDOW_V1,
            )


class BrowserCaptureBindingTests(unittest.TestCase):
    def test_feed_capture_binding_replaces_video_input_with_actual_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifacts = ArtifactStore(Path(directory) / "artifacts")
            source = Path(directory) / "feed.mp4"
            source.write_bytes(b"video")

            class FakeExtractor:
                def extract(self, **_kwargs):
                    stored = artifacts.put_bytes(
                        "captures", "realtime/feed-a/capture.jpg", b"\xff\xd8\xffcapture"
                    )
                    return {
                        **stored,
                        "source_asset_id": "feed-a",
                        "timestamp_s": 4.25,
                        "capture_policy": SEEDED_RANDOM_SAFE_WINDOW_V1,
                    }

            harness = BrowserRealtimeHarness(
                Path(directory),
                artifacts,
                repository=object(),
                capture_extractor=FakeExtractor(),
            )
            path, capture = harness._prepare_input(
                {
                    "case_id": "case-a",
                    "api_asset_bindings": {
                        "input_media_role": "feed_capture",
                        "capture_frame_policy": SEEDED_RANDOM_SAFE_WINDOW_V1,
                    },
                },
                {"asset_id": "feed-a", "kind": "feed_video"},
                source,
            )
            self.assertTrue(path.name.endswith(".jpg"))
            self.assertEqual(capture["timestamp_s"], 4.25)


if __name__ == "__main__":
    unittest.main()
