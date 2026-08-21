"""P5 Realtime generation tests (Python controller + fake harness)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from xmax_test.errors import ContractError
from xmax_test.generation.realtime.controller import (
    FakeRealtimeHarness,
    RealtimeController,
)
from xmax_test.generation.realtime.interactions import InteractionProfileResolver
from xmax_test.storage.artifacts import ArtifactStore
from xmax_test.storage.sqlite import SqliteMetadataRepository
from xmax_test.time import FixedClock


class RealtimeTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.repository = SqliteMetadataRepository(root / "db.sqlite3", clock=FixedClock())
        self.artifacts = ArtifactStore(root / "artifacts")
        self.clock = FixedClock()
        self.controller = RealtimeController(self.repository, self.artifacts, clock=self.clock)

    def tearDown(self) -> None:
        self.repository.close()
        self.directory.cleanup()

    def case(self, **extra) -> dict:
        case: dict = {
            "case_id": "case-rt",
            "case_number": "feed001_prompt003_01",
            "feed_asset_id": "feed-a",
            "prompt_asset_ids": [],
            "prompt_text": "触控：跟随手指",
            "generation_mode": "realtime",
            "model_id": "x2.0",
            "operation_recipe_id": "realtime-track-interaction",
            "operation_recipe_version": "0.1.0",
            "edited_video_asset_id": "feed-a",
            "expected_audio_source_asset_id": "feed-a",
            "api_asset_bindings": {
                "input_method": "connectMedia",
                "ref_image_role": "none",
                "interaction_profile_id": "pointer-track-30fps-v1",
            },
        }
        case.update(extra)
        return case


class RealtimeControllerTests(RealtimeTestBase):
    def test_versioned_pointer_profile_expands_to_30fps_tracks(self) -> None:
        resolver = InteractionProfileResolver(
            {
                "profiles": [
                    {
                        "profile_id": "p",
                        "version": "1",
                        "event_kind": "pointer_tracks",
                        "sample_fps": 30,
                        "segments": [
                            {
                                "start_ms": 0,
                                "duration_ms": 1000,
                                "from": [0, 0],
                                "to": [1, 1],
                                "fingers": 2,
                            }
                        ],
                    }
                ]
            }
        )
        expanded = resolver.expand("p", width=100, height=50)
        self.assertEqual(len(expanded["tracks"]), 31)
        self.assertEqual(expanded["tracks"][0]["points"][0], [0, 0])
        self.assertEqual(expanded["tracks"][-1]["points"][0], [100, 50])
        self.assertEqual(len(expanded["tracks"][0]["points"]), 2)

    def test_run_case_persists_completed_realtime_run(self) -> None:
        run = self.controller.run_case(self.case())
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["origin"], "xmax_realtime")
        self.assertEqual(run["metrics"]["input_method"], "connectMedia")
        self.assertEqual(run["metrics"]["single_round"], True)
        self.assertEqual(run["metrics"]["audio"], {"publish": True, "subscribe": True})
        self.assertIsNotNone(run["raw_events_uri"])
        events = self.repository.get_event_log(run["run_id"])
        self.assertEqual(events[0]["event"], "run_created")

    def test_single_round_not_polluted_by_auto_loop(self) -> None:
        harness = FakeRealtimeHarness(clock=self.clock, fps=30)
        controller = RealtimeController(
            self.repository, self.artifacts, harness=harness, clock=self.clock
        )
        run = controller.run_case(self.case(), config={"duration_s": 1.0})
        # The harness only records one task_start/task_stop pair.
        raw = self.artifacts.read_bytes(run["raw_events_uri"]).decode("utf-8")
        import json

        payload = json.loads(raw)
        starts = [e for e in payload["events"] if e["event"] == "task_start"]
        stops = [e for e in payload["events"] if e["event"] == "task_stop"]
        self.assertEqual(len(starts), 1)
        self.assertEqual(len(stops), 1)
        self.assertEqual(payload["single_round"], True)

    def test_tracks_frames_are_30fps_and_mapped_to_content_resolution(self) -> None:
        harness = FakeRealtimeHarness(clock=self.clock, fps=30)
        controller = RealtimeController(
            self.repository, self.artifacts, harness=harness, clock=self.clock
        )
        run = controller.run_case(
            self.case(),
            config={
                "duration_s": 2.0,
                "content_width": 1280,
                "content_height": 720,
                "dom_width": 640,
                "dom_height": 360,
            },
        )
        raw = self.artifacts.read_bytes(run["raw_events_uri"]).decode("utf-8")
        import json

        payload = json.loads(raw)
        frames = [e for e in payload["events"] if e["event"] == "tracks_frame"]
        self.assertEqual(len(frames), 60)  # 2s at 30 FPS
        intervals = [
            frames[i + 1]["plannedMs"] - frames[i]["plannedMs"] for i in range(len(frames) - 1)
        ]
        self.assertTrue(all(abs(interval - 1000 / 30) < 1 for interval in intervals))
        first = frames[0]
        # Screen (0, 50) on a 640x360 DOM -> content (0, 100) on 1280x720.
        self.assertEqual(first["screenCoords"], [[0, 50]])
        self.assertEqual(first["contentCoords"], [[0, 100]])

    def test_audio_is_explicitly_published_and_subscribed(self) -> None:
        harness = FakeRealtimeHarness(clock=self.clock)
        controller = RealtimeController(
            self.repository, self.artifacts, harness=harness, clock=self.clock
        )
        run = controller.run_case(self.case())
        self.assertEqual(run["metrics"]["audio"]["publish"], True)
        self.assertEqual(run["metrics"]["audio"]["subscribe"], True)

    def test_all_callbacks_have_timestamps(self) -> None:
        harness = FakeRealtimeHarness(clock=self.clock)
        controller = RealtimeController(
            self.repository, self.artifacts, harness=harness, clock=self.clock
        )
        run = controller.run_case(self.case())
        raw = self.artifacts.read_bytes(run["raw_events_uri"]).decode("utf-8")
        import json

        payload = json.loads(raw)
        for callback in payload["callbacks"]:
            self.assertIn("tsMonotonicMs", callback)
            self.assertIn("tsWallMs", callback)

    def test_disconnect_reconnect_is_supported(self) -> None:
        harness = FakeRealtimeHarness(clock=self.clock)
        controller = RealtimeController(
            self.repository, self.artifacts, harness=harness, clock=self.clock
        )
        run = controller.run_case(
            self.case(),
            config={"simulate_disconnect": True, "simulate_reconnect": True},
        )
        raw = self.artifacts.read_bytes(run["raw_events_uri"]).decode("utf-8")
        import json

        payload = json.loads(raw)
        states = [item["state"] for item in payload["state_changes"]]
        self.assertIn("disconnected", states)
        # Reconnect leads back to running and a new video_started event.
        self.assertEqual(states[-1], "running")
        video_started = [c for c in payload["callbacks"] if c.get("event") == "video_started"]
        self.assertEqual(len(video_started), 2)

    def test_invalid_input_method_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            self.controller.run_case(self.case(api_asset_bindings={"input_method": "bogus"}))

    def test_no_real_key_needed_for_tests(self) -> None:
        # Constructing and running the controller never touches XMAX credentials.
        run = self.controller.run_case(self.case())
        self.assertEqual(run["status"], "completed")


if __name__ == "__main__":
    unittest.main()
