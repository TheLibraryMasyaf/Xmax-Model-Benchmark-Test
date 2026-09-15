"""Decart Lucy 2.5 offline adapter tests; no network or paid request."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from xmax_test.assets.validator import MediaValidator
from xmax_test.errors import AmbiguousSubmissionError, MissingDependencyError, ValidationError
from xmax_test.generation.offline.decart import (
    DecartOfflineGenerationAdapter,
    FakeDecartQueueTransport,
    HttpDecartQueueTransport,
)
from xmax_test.generation.offline.media import FfmpegOfflineInputNormalizer
from xmax_test.generation.offline.repository import OfflineRunRepository
from xmax_test.storage.artifacts import ArtifactStore
from xmax_test.storage.sqlite import SqliteMetadataRepository
from xmax_test.time import FixedClock


class H264Probe:
    def probe(self, path: Path):
        return {
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 1280,
                    "height": 720,
                    "avg_frame_rate": "24/1",
                }
            ],
            "format": {"format_name": "mov,mp4", "duration": "4.0"},
        }


class LongH264Probe:
    def __init__(self, duration_s: float = 120.0) -> None:
        self.duration_s = duration_s

    def probe(self, path: Path):
        return {
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 1280,
                    "height": 720,
                    "avg_frame_rate": "24/1",
                    "duration": str(self.duration_s),
                },
                {"codec_type": "audio", "codec_name": "aac", "duration": str(self.duration_s)},
            ],
            "format": {"format_name": "mov,mp4", "duration": str(self.duration_s)},
        }


class IdentityNormalizer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def normalize(self, asset: dict, profile: str) -> dict:
        self.calls.append((asset["asset_id"], profile))
        return {**asset, "normalization": {"profile": profile}}


class DecartAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.database = SqliteMetadataRepository(root / "db.sqlite3", clock=FixedClock())
        self.artifacts = ArtifactStore(root / "artifacts")
        self.repository = OfflineRunRepository(self.database, self.artifacts)
        for asset_id, kind in (
            ("feed", "feed_video"),
            ("prompt-image", "prompt_image"),
            ("prompt-video", "prompt_video"),
        ):
            uri = f"artifact://assets/{asset_id}/source.bin"
            path = self.artifacts.resolve(uri)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"media-{asset_id}".encode())
            self.database.upsert_asset(
                {
                    "asset_id": asset_id,
                    "kind": kind,
                    "uri": uri,
                    "sha256": f"sha-{asset_id}",
                    "bytes": path.stat().st_size,
                    "status": "ready",
                    "media": {"width": 1280, "height": 720, "duration_s": 4.0},
                }
            )
        self.normalizer = IdentityNormalizer()

    def tearDown(self) -> None:
        self.database.close()
        self.directory.cleanup()

    def case(self, *, video_reference: bool = False) -> dict:
        return {
            "case_id": "case-video" if video_reference else "case-image",
            "case_number": "feed001_prompt002" if video_reference else "feed001_prompt001",
            "feed_asset_id": "feed",
            "prompt_asset_ids": ["prompt-video" if video_reference else "prompt-image"],
            "prompt_text": "follow the reference",
            "generation_mode": "offline",
            "generation_provider": "decart",
            "model_id": "lucy-2.5",
            "operation_recipe_id": (
                "offline-video-reference-with-feed-capture"
                if video_reference
                else "offline-image-reference"
            ),
            "operation_recipe_version": "1",
            "edited_video_asset_id": "prompt-video" if video_reference else "feed",
            "expected_audio_source_asset_id": "prompt-video" if video_reference else "feed",
            "api_asset_bindings": {
                "refVideoPath": "prompt_video" if video_reference else "feed_video",
                "refImagePath": "feed_capture" if video_reference else "prompt_image",
            },
            "generation_config": {
                "input_normalization_profile": "decart-720p-h264-pad-v1",
                "resolution": "720p",
                "enhance_prompt": False,
                "self_anchor": True,
                "seed": 7,
            },
        }

    def adapter(self, transport: FakeDecartQueueTransport) -> DecartOfflineGenerationAdapter:
        from xmax_test.generation.feed_input import FakeFeedPreprocessor
        return DecartOfflineGenerationAdapter(
            self.repository,
            self.artifacts,
            MediaValidator(probe=H264Probe()),
            self.repository,
            transport=transport,
            input_normalizer=self.normalizer,
            feed_preprocessor=FakeFeedPreprocessor(),
            model_id="lucy-2.5",
            run_batch_id="runs-decart",
            poll_interval_s=0,
            clock=FixedClock(),
            capture_extractor=lambda feed: {**feed, "kind": "feed_image"},
        )

    def test_image_reference_maps_to_lucy_multipart_fields(self) -> None:
        transport = FakeDecartQueueTransport()
        run = self.adapter(transport).run_case(self.case())
        submitted = transport.submissions[-1]
        self.assertEqual(submitted["video_path"], self.repository.get_asset("feed")["uri"].replace("artifact://", str(self.artifacts.root) + "/"))
        self.assertTrue(submitted["reference_image_path"].endswith("prompt-image/source.bin"))
        self.assertEqual(submitted["resolution"], "720p")
        self.assertEqual(submitted["seed"], 7)
        self.assertFalse(submitted["enhance_prompt"])
        self.assertTrue(submitted["self_anchor"])
        self.assertEqual(run["origin"], "decart_offline")
        self.assertEqual(run["metrics"]["provider"], "decart")
        self.assertTrue(run["raw_events_uri"])
        self.assertEqual(self.normalizer.calls[0][0], "feed")

    def test_video_reference_uses_prompt_video_and_feed_capture(self) -> None:
        transport = FakeDecartQueueTransport()
        run = self.adapter(transport).run_case(self.case(video_reference=True))
        submitted = transport.submissions[-1]
        self.assertTrue(submitted["video_path"].endswith("prompt-video/source.bin"))
        self.assertTrue(submitted["reference_image_path"].endswith("feed/source.bin"))
        self.assertEqual(run["expected_audio_source_asset_id"], "prompt-video")

    def test_inflight_job_is_polled_without_resubmit(self) -> None:
        case = self.case()
        self.repository.create_run(
            {
                "run_id": "run-existing",
                "run_batch_id": "runs-decart",
                "case_id": case["case_id"],
                "case_number": case["case_number"],
                "status": "running",
                "model_id": "lucy-2.5",
                "mode": "offline",
                "origin": "decart_offline",
                "provenance": {},
                "metrics": {"provider": "decart"},
            }
        )
        self.repository.append_event(
            "run-existing", "task_submitted", payload={"external_task_id": "job-existing"}
        )
        transport = FakeDecartQueueTransport()
        run = self.adapter(transport).run_case(case)
        self.assertEqual(run["run_id"], "run-existing")
        self.assertEqual(transport.submissions, [])

    def test_ambiguous_create_is_not_retried_or_marked_terminal(self) -> None:
        transport = FakeDecartQueueTransport(
            submit_error=AmbiguousSubmissionError("response lost")
        )
        with self.assertRaises(AmbiguousSubmissionError):
            self.adapter(transport).run_case(self.case())
        runs = self.database.list_runs()
        self.assertEqual(len(transport.submissions), 1)
        self.assertEqual(runs[0]["status"], "running")
        self.assertEqual(runs[0]["metrics"]["failure_class"], "ambiguous_submission")

    def test_real_transport_preflight_requires_token_without_network(self) -> None:
        transport = HttpDecartQueueTransport(api_key=None)
        with self.assertRaises(MissingDependencyError):
            transport.preflight()


class NormalizerTests(unittest.TestCase):
    def test_ffmpeg_normalization_is_cached_and_validated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mov"
            source.write_bytes(b"source")
            artifacts = ArtifactStore(root / "artifacts")
            calls: list[list[str]] = []

            def runner(command, **kwargs):
                calls.append(command)
                Path(command[-1]).write_bytes(b"normalized")
                return type("Completed", (), {"returncode": 0, "stderr": ""})()

            normalizer = FfmpegOfflineInputNormalizer(
                artifacts, MediaValidator(probe=H264Probe()), runner=runner
            )
            asset = {
                "asset_id": "source",
                "path": str(source),
                "sha256": "source-sha",
                "kind": "feed_video",
                "media": {"width": 720, "height": 1280},
            }
            first = normalizer.normalize(asset, normalizer.PROFILE)
            second = normalizer.normalize(asset, normalizer.PROFILE)
            self.assertEqual(len(calls), 1)
            self.assertIn("pad=720:1280", " ".join(calls[0]))
            self.assertEqual(first["sha256"], second["sha256"])
            self.assertEqual(first["media"]["video_codec"], "h264")

    def test_long_video_profile_controls_bitrate_and_records_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "long-source.mov"
            source.write_bytes(b"source")
            artifacts = ArtifactStore(root / "artifacts")
            calls: list[tuple[list[str], dict]] = []

            def runner(command, **kwargs):
                calls.append((command, kwargs))
                Path(command[-1]).write_bytes(b"normalized")
                return type("Completed", (), {"returncode": 0, "stderr": ""})()

            normalizer = FfmpegOfflineInputNormalizer(
                artifacts, MediaValidator(probe=LongH264Probe()), runner=runner
            )
            asset = {
                "asset_id": "long-source",
                "path": str(source),
                "sha256": "long-source-sha",
                "kind": "feed_video",
                "media": {"width": 1920, "height": 1080, "duration_s": 120.0, "has_audio": True},
            }
            result = normalizer.normalize(asset, normalizer.LONG_VIDEO_PROFILE)
            command, kwargs = calls[0]
            self.assertIn("-b:v", command)
            self.assertIn("-maxrate", command)
            self.assertEqual(kwargs["timeout"], 2400)
            self.assertEqual(result["normalization"]["input_sha256"], "long-source-sha")
            self.assertEqual(
                result["normalization"]["command"][-1],
                str(artifacts.resolve(result["path"].replace("artifact://", "")))
                if result["path"].startswith("artifact://")
                else result["path"],
            )
            self.assertEqual(result["media"]["has_audio"], True)

    def test_v3_uses_crf_with_maxrate_without_forced_abr(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "short-source.mov"
            source.write_bytes(b"source")
            artifacts = ArtifactStore(root / "artifacts")
            calls: list[list[str]] = []

            def runner(command, **kwargs):
                calls.append(command)
                Path(command[-1]).write_bytes(b"normalized")
                return type("Completed", (), {"returncode": 0, "stderr": ""})()

            normalizer = FfmpegOfflineInputNormalizer(
                artifacts, MediaValidator(probe=LongH264Probe(11.0)), runner=runner
            )
            result = normalizer.normalize(
                {
                    "asset_id": "short-source",
                    "path": str(source),
                    "sha256": "short-source-sha",
                    "kind": "feed_video",
                    "media": {"width": 1920, "height": 1080, "duration_s": 11.0, "has_audio": True},
                },
                normalizer.CRF_CAPPED_PROFILE,
            )
            command = calls[0]
            self.assertIn("-crf", command)
            self.assertIn("18", command)
            self.assertIn("-maxrate", command)
            self.assertIn("8000k", command)
            self.assertNotIn("-b:v", command)
            self.assertEqual(result["normalization"]["profile"], normalizer.CRF_CAPPED_PROFILE)

    def test_v3_size_guard_rejects_oversized_encode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "oversized-source.mov"
            source.write_bytes(b"source")
            artifacts = ArtifactStore(root / "artifacts")

            def runner(command, **kwargs):
                with Path(command[-1]).open("wb") as handle:
                    handle.truncate(200_000_000)
                return type("Completed", (), {"returncode": 0, "stderr": ""})()

            normalizer = FfmpegOfflineInputNormalizer(
                artifacts, MediaValidator(probe=LongH264Probe()), runner=runner
            )
            with self.assertRaises(ValidationError):
                normalizer.normalize(
                    {
                        "asset_id": "oversized-source",
                        "path": str(source),
                        "sha256": "oversized-source-sha",
                        "kind": "feed_video",
                        "media": {"width": 1920, "height": 1080, "duration_s": 120.0, "has_audio": True},
                    },
                    normalizer.CRF_CAPPED_PROFILE,
                )


class HttpTransportContractTests(unittest.TestCase):
    def test_submit_uses_documented_endpoint_headers_and_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "input.mp4"
            image = root / "reference.png"
            video.write_bytes(b"video")
            image.write_bytes(b"\x89PNG\r\n\x1a\nimage")

            class Response:
                status = 200

                @staticmethod
                def read():
                    return b'{"job_id":"job-contract","status":"queued"}'

            class Connection:
                def __init__(self):
                    self.method = None
                    self.path = None
                    self.headers = {}
                    self.body = bytearray()

                def putrequest(self, method, path):
                    self.method, self.path = method, path

                def putheader(self, name, value):
                    self.headers[name.lower()] = value

                def endheaders(self):
                    return None

                def send(self, block):
                    self.body.extend(block)

                def getresponse(self):
                    return Response()

                def close(self):
                    return None

            connection = Connection()
            transport = HttpDecartQueueTransport(api_key="secret-test-only")
            transport._connection = lambda: connection
            result = transport.submit(
                video_path=str(video),
                reference_image_path=str(image),
                prompt="replace subject",
                seed=9,
                resolution="720p",
                enhance_prompt=False,
                self_anchor=True,
            )
            body = bytes(connection.body)
            self.assertEqual(result["job_id"], "job-contract")
            self.assertEqual((connection.method, connection.path), ("POST", "/v1/jobs/lucy-2.5"))
            self.assertEqual(connection.headers["x-api-key"], "secret-test-only")
            for name in (
                b'name="data"',
                b'name="prompt"',
                b'name="reference_image"',
                b'name="seed"',
                b'name="resolution"',
                b'name="enhance_prompt"',
                b'name="self_anchor"',
            ):
                self.assertIn(name, body)

    def test_poll_maps_provider_failed_to_internal_error(self) -> None:
        transport = HttpDecartQueueTransport(api_key="secret-test-only")
        transport._get_json = lambda path: {"status": "failed", "error": "model failed"}
        state = transport.poll("job-failed")
        self.assertEqual(state["status"], "error")
        self.assertEqual(state["provider_status"], "failed")

    def test_success_with_invalid_json_is_ambiguous_and_never_retried(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "input.mp4"
            video.write_bytes(b"video")

            class Connection:
                def __init__(self, body):
                    self.body = body
                    self.send_count = 0

                def putrequest(self, method, path):
                    return None

                def putheader(self, name, value):
                    return None

                def endheaders(self):
                    return None

                def send(self, block):
                    self.send_count += 1

                def getresponse(self):
                    body = self.body

                    class Response:
                        status = 201

                        def read(self):
                            return body

                    return Response()

                def close(self):
                    return None

            for body in (b"not-json", b"[1, 2, 3]"):
                connection = Connection(body)
                transport = HttpDecartQueueTransport(api_key="secret-test-only")
                transport._connection = lambda connection=connection: connection
                with self.assertRaises(AmbiguousSubmissionError):
                    transport.submit(video_path=str(video), prompt="replace subject")
                self.assertGreater(connection.send_count, 0)


if __name__ == "__main__":
    unittest.main()
