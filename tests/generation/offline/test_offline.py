"""P4 Offline generation tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from xmax_test.assets.validator import MediaValidator
from xmax_test.errors import ContractError, ExternalServiceError, ValidationError
from xmax_test.generation.fakes import build_offline_adapter
from xmax_test.generation.offline.repository import OfflineRunRepository
from xmax_test.generation.offline.rest_adapter import (
    FakeOfflineTaskTransport,
    HttpOfflineTaskTransport,
)
from xmax_test.generation.offline.rtc_adapter import FakeRtcAdapter
from xmax_test.generation.offline.session_api import FakeSessionApiClient
from xmax_test.storage.artifacts import ArtifactStore
from xmax_test.storage.sqlite import SqliteMetadataRepository
from xmax_test.time import FixedClock


class FakeProbe:
    def __init__(self, corrupt: bool = False) -> None:
        self._corrupt = corrupt

    def probe(self, path: Path):
        if self._corrupt or path.stat().st_size == 0:
            raise ValidationError("cannot decode")
        return {
            "streams": [{"codec_type": "video", "width": 704, "height": 1280, "avg_frame_rate": "24/1"}],
            "format": {"format_name": "mp4", "duration": "8.0"},
        }


class OfflineTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.repository = SqliteMetadataRepository(root / "db.sqlite3", clock=FixedClock())
        self.artifacts = ArtifactStore(root / "artifacts")
        self.run_repo = OfflineRunRepository(self.repository, self.artifacts)
        self._register_assets()
        self.clock = FixedClock()

    def tearDown(self) -> None:
        self.repository.close()
        self.directory.cleanup()

    def _register_assets(self) -> None:
        for asset_id, kind in [
            ("feed-a", "feed_video"),
            ("prompt-img", "prompt_image"),
            ("prompt-vid", "prompt_video"),
        ]:
            self.repository.upsert_asset(
                {
                    "asset_id": asset_id,
                    "kind": kind,
                    "uri": f"artifact://assets/{asset_id}/source.bin",
                    "sha256": f"sha-{asset_id}",
                    "bytes": 8,
                    "status": "ready",
                }
            )
            (self.artifacts.resolve(f"artifact://assets/{asset_id}/source.bin")).parent.mkdir(
                parents=True, exist_ok=True
            )
            (self.artifacts.resolve(f"artifact://assets/{asset_id}/source.bin")).write_bytes(b"x" * 8)

    def image_case(self) -> dict:
        return {
            "case_id": "case-img",
            "case_number": "feed001_prompt001_01",
            "feed_asset_id": "feed-a",
            "prompt_asset_ids": ["prompt-img"],
            "prompt_text": "换装",
            "generation_mode": "offline",
            "model_id": "x2.0",
            "operation_recipe_id": "offline-image-reference",
            "operation_recipe_version": "0.1.0",
            "edited_video_asset_id": "feed-a",
            "expected_audio_source_asset_id": "feed-a",
            "api_asset_bindings": {"refVideoPath": "feed_video", "refImagePath": "prompt_image"},
        }

    def video_case(self) -> dict:
        return {
            "case_id": "case-vid",
            "case_number": "feed001_prompt002_01",
            "feed_asset_id": "feed-a",
            "prompt_asset_ids": ["prompt-vid"],
            "prompt_text": "手势舞",
            "generation_mode": "offline",
            "model_id": "x2.0",
            "operation_recipe_id": "offline-video-reference-with-feed-capture",
            "operation_recipe_version": "0.1.0",
            "edited_video_asset_id": "prompt-vid",
            "expected_audio_source_asset_id": "prompt-vid",
            "api_asset_bindings": {"refVideoPath": "prompt_video", "refImagePath": "feed_capture"},
        }

    def adapter(self, **kwargs):
        return build_offline_adapter(
            self.run_repo,
            self.artifacts,
            MediaValidator(probe=FakeProbe()),
            self.run_repo,
            clock=self.clock,
            **kwargs,
        )


class RestBindingTests(OfflineTestBase):
    def test_extensionless_assets_get_official_media_types(self) -> None:
        root = Path(self.directory.name)
        png = root / "image.bin"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 24)
        mp4 = root / "video.bin"
        mp4.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"x" * 20)
        mov = root / "movie.bin"
        mov.write_bytes(b"\x00\x00\x00\x18ftypqt  " + b"x" * 20)

        self.assertEqual(
            HttpOfflineTaskTransport._detect_media(str(png), "image/"),
            ("image/png", "png"),
        )
        self.assertEqual(
            HttpOfflineTaskTransport._detect_media(str(mp4), "video/"),
            ("video/mp4", "mp4"),
        )
        self.assertEqual(
            HttpOfflineTaskTransport._detect_media(str(mov), "video/"),
            ("video/quicktime", "mov"),
        )

    def test_official_upload_url_prefers_cos_location_then_sts_endpoint(self) -> None:
        sts = {
            "bucket": "bucket-123",
            "region": "ap-test",
            "endpoint": "cos.ap-test.myqcloud.com",
        }
        self.assertEqual(
            HttpOfflineTaskTransport._resolve_cos_upload_url(
                {"Location": "cdn.example.com/path/video.mp4"}, sts, "ignored"
            ),
            "https://cdn.example.com/path/video.mp4",
        )
        self.assertEqual(
            HttpOfflineTaskTransport._resolve_cos_upload_url({}, sts, "prefix/video 1.mp4"),
            "https://bucket-123.cos.ap-test.myqcloud.com/prefix/video%201.mp4",
        )

    def test_real_transport_projects_only_official_submit_fields(self) -> None:
        transport = HttpOfflineTaskTransport(
            base_url="https://example.invalid/open/api/v1", quality="hd", fps=24
        )
        captured = {}

        def request(method, path, payload=None):
            captured.update({"method": method, "path": path, "payload": payload})
            return {"uid": "task-real-shape"}

        transport._request = request
        result = transport.submit(
            {
                "taskUid": "local-only",
                "model": "x2.0",
                "prompt": "change clothes",
                "refVideoPath": "https://example.invalid/feed.mp4",
                "refImagePath": "https://example.invalid/ref.png",
                "audioBaselineAssetId": "asset-local-only",
            }
        )
        self.assertEqual(result["taskUid"], "task-real-shape")
        self.assertEqual(
            captured["payload"],
            {
                "prompt": "change clothes",
                "refVideoPath": "https://example.invalid/feed.mp4",
                "refImagePath": "https://example.invalid/ref.png",
                "quality": "hd",
                "fps": 24,
            },
        )

    def test_image_reference_binds_feed_as_video_and_prompt_image_as_image(self) -> None:
        transport = FakeOfflineTaskTransport()
        adapter = self.adapter(transport=transport)
        run = adapter.run_case(self.image_case())
        self.assertEqual(run["status"], "completed")
        from xmax_test.hashing import sha256_bytes

        self.assertEqual(
            run["result_asset_id"], f"asset_{sha256_bytes(b'fake-result-video')[:16]}"
        )
        submitted = transport.submitted_payloads[-1]
        self.assertEqual(submitted["refVideoPath"], f"https://assets.example.invalid/sha-feed-a")
        self.assertEqual(submitted["refImagePath"], "https://assets.example.invalid/sha-prompt-img")
        self.assertEqual(submitted["audioBaselineAssetId"], "feed-a")

    def test_video_reference_binds_prompt_video_as_video_and_feed_capture_as_image(self) -> None:
        transport = FakeOfflineTaskTransport()
        adapter = self.adapter(transport=transport)
        run = adapter.run_case(self.video_case())
        self.assertEqual(run["status"], "completed")
        submitted = transport.submitted_payloads[-1]
        self.assertEqual(submitted["refVideoPath"], "https://assets.example.invalid/sha-prompt-vid")
        self.assertEqual(submitted["refImagePath"], "https://assets.example.invalid/sha-feed-a")
        self.assertEqual(submitted["audioBaselineAssetId"], "prompt-vid")
        self.assertEqual(run["metrics"]["audio_facts"]["baseline_recorded"], True)

    def test_audio_baseline_is_saved_on_run(self) -> None:
        adapter = self.adapter()
        run = adapter.run_case(self.image_case())
        self.assertEqual(run["expected_audio_source_asset_id"], "feed-a")

    def test_missing_prompt_image_for_image_reference_is_rejected(self) -> None:
        case = self.image_case()
        case["prompt_asset_ids"] = []
        adapter = self.adapter()
        with self.assertRaises(ValidationError):
            adapter.run_case(case)

    def test_missing_asset_raises_contract_error(self) -> None:
        case = self.image_case()
        case["feed_asset_id"] = "missing-asset"
        adapter = self.adapter()
        with self.assertRaises(ContractError):
            adapter.run_case(case)


class RestFailureTests(OfflineTestBase):
    def test_submit_failure_persists_terminal_error_run(self) -> None:
        transport = FakeOfflineTaskTransport(submit_error="rejected before billing")
        adapter = self.adapter(transport=transport)
        with self.assertRaises(ExternalServiceError):
            adapter.run_case(self.image_case())
        rows = self.repository.list_runs()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "error")
        self.assertEqual(rows[0]["metrics"]["failure_class"], "submit_failure")

    def test_model_error_sets_failure_class(self) -> None:
        transport = FakeOfflineTaskTransport(
            poll_states=[
                {"status": "submitted"},
                {"status": "error", "message": "model exploded"},
            ]
        )
        adapter = self.adapter(transport=transport)
        run = adapter.run_case(self.image_case())
        self.assertEqual(run["status"], "error")
        self.assertEqual(run["metrics"].get("failure_class"), "model_error")

    def test_corrupt_download_becomes_download_failure(self) -> None:
        transport = FakeOfflineTaskTransport()
        adapter = self.adapter(
            transport=transport,
            result_bytes=b"",
        )
        # Empty payload cannot pass media validation -> download/validation failure.
        adapter._validator = MediaValidator(probe=FakeProbe(corrupt=True))
        run = adapter.run_case(self.image_case())
        self.assertEqual(run["status"], "error")

    def test_duplicate_submission_is_protected(self) -> None:
        adapter = self.adapter()
        first = adapter.run_case(self.image_case())
        submitted = adapter._transport.submitted_payloads
        # A second attempt creates a new run (new attempt) but never reuses
        # the same external task id.
        second = adapter.run_case(self.image_case())
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertNotEqual(
            first["metrics"]["external_task_id"], second["metrics"]["external_task_id"]
        )

    def test_resume_skips_completed_case(self) -> None:
        adapter = self.adapter()
        first = adapter.run_case(self.image_case())
        self.assertEqual(first["status"], "completed")
        # A completed run for the same case+model is found; the run is stable.
        found = self.run_repo.completed_run_for_case("case-img", "x2.0")
        self.assertEqual(found["run_id"], first["run_id"])


class SessionRtcTests(OfflineTestBase):
    def test_session_success(self) -> None:
        session_api = FakeSessionApiClient()
        rtc = FakeRtcAdapter()
        adapter = self.adapter(backend="session_rtc", session_api=session_api, rtc=rtc)
        run = adapter.run_case(self.image_case())
        self.assertEqual(run["status"], "completed")
        self.assertIn("close_session", session_api.calls)
        self.assertTrue(rtc.closed)
        self.assertEqual(rtc.started_tasks, [f"task-{run['run_id'][-12:]}"])

    def test_session_closes_even_on_model_error(self) -> None:
        session_api = FakeSessionApiClient()
        rtc = FakeRtcAdapter(
            events=[{"event": "error", "payload": {"taskUid": "T", "message": "boom"}}]
        )
        adapter = self.adapter(backend="session_rtc", session_api=session_api, rtc=rtc)
        run = adapter.run_case(self.image_case())
        self.assertEqual(run["status"], "error")
        self.assertIn("close_session", session_api.calls)
        self.assertTrue(rtc.closed)

    def test_heartbeat_failure_before_video_started_fails_run(self) -> None:
        session_api = FakeSessionApiClient(fail_heartbeat="heartbeat down")
        rtc = FakeRtcAdapter(
            events=[],  # never emits video_started so heartbeat error surfaces
        )
        adapter = self.adapter(backend="session_rtc", session_api=session_api, rtc=rtc)
        run = adapter.run_case(self.image_case())
        self.assertEqual(run["status"], "error")
        self.assertIn("heartbeat_failure", str(run["metrics"].get("failure_class", "")))

    def test_ready_timeout_fails_and_closes_session(self) -> None:
        session_api = FakeSessionApiClient()
        rtc = FakeRtcAdapter(never_ready=True, ready_timeout_s=0.01)
        adapter = self.adapter(backend="session_rtc", session_api=session_api, rtc=rtc)
        run = adapter.run_case(self.image_case())
        self.assertEqual(run["status"], "error")
        self.assertIn("close_session", session_api.calls)

    def test_join_failure_never_reaches_start(self) -> None:
        session_api = FakeSessionApiClient()
        rtc = FakeRtcAdapter(join_fails=True)
        adapter = self.adapter(backend="session_rtc", session_api=session_api, rtc=rtc)
        run = adapter.run_case(self.image_case())
        self.assertEqual(run["status"], "error")
        self.assertIn("rtc_join_failure", run["metrics"].get("failure_class", ""))
        self.assertEqual(rtc.started_tasks, [])


if __name__ == "__main__":
    unittest.main()
