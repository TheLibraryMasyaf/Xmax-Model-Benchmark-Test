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
from xmax_test.config import load_config
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
    @staticmethod
    def network_profile() -> dict:
        root = Path(__file__).resolve().parents[3]
        pack = load_config(
            root / "config" / "network-profiles.json",
            "network-profiles.schema.json",
            base_dir=root,
        )
        return pack["profiles"][0]

    @staticmethod
    def randomized_profile() -> dict:
        return {
            "profiles": [
                {
                    "profile_id": "random-swipes",
                    "version": "2",
                    "event_kind": "pointer_tracks",
                    "sample_fps": 30,
                    "segments": [],
                    "randomization": {
                        "kind": "seeded_user_swipes",
                        "seed_scope": "case",
                        "swipe_count": [4, 6],
                        "start_delay_ms": [180, 360],
                        "duration_ms": [260, 520],
                        "gap_ms": [80, 200],
                        "end_padding_ms": 180,
                        "edge_margin": 0.08,
                        "distance": [0.22, 0.72],
                        "curvature": [-0.18, 0.18],
                        "jitter": 0.008,
                        "easing": ["ease_in_out", "ease_out", "linear"],
                    },
                }
            ]
        }

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

    def test_seeded_user_swipes_are_varied_reproducible_and_bounded(self) -> None:
        resolver = InteractionProfileResolver(self.randomized_profile())
        first = resolver.expand(
            "random-swipes", width=1280, height=720, seed_key="case-a", duration_ms=3000
        )["tracks"]
        repeated = resolver.expand(
            "random-swipes", width=1280, height=720, seed_key="case-a", duration_ms=3000
        )["tracks"]
        different = resolver.expand(
            "random-swipes", width=1280, height=720, seed_key="case-b", duration_ms=3000
        )["tracks"]

        self.assertEqual(first, repeated)
        self.assertNotEqual(first, different)
        swipe_ids = list(dict.fromkeys(frame["swipe_id"] for frame in first))
        self.assertGreaterEqual(len(swipe_ids), 4)
        self.assertLessEqual(len(swipe_ids), 6)
        for swipe_id in swipe_ids:
            frames = [frame for frame in first if frame["swipe_id"] == swipe_id]
            self.assertEqual(frames[0]["phase"], "start")
            self.assertEqual(frames[-1]["phase"], "end")
        self.assertLessEqual(first[-1]["at_ms"], 3000)
        self.assertTrue(
            all(
                0 <= coordinate <= limit
                for frame in first
                for point in frame["points"]
                for coordinate, limit in zip(point, (1280, 720), strict=True)
            )
        )

        endpoints = []
        for swipe_id in swipe_ids:
            frames = [frame for frame in first if frame["swipe_id"] == swipe_id]
            start = frames[0]["points"][0]
            end = frames[-1]["points"][0]
            endpoints.append((end[0] - start[0], end[1] - start[1]))
        direction_quadrants = {(dx >= 0, dy >= 0) for dx, dy in endpoints}
        self.assertGreaterEqual(len(direction_quadrants), 3)

    def test_random_swipe_profile_rejects_reversed_ranges(self) -> None:
        pack = self.randomized_profile()
        pack["profiles"][0]["randomization"]["swipe_count"] = [6, 4]
        resolver = InteractionProfileResolver(pack)
        with self.assertRaises(ContractError):
            resolver.expand("random-swipes", width=1280, height=720, seed_key="case-a")

    def test_run_case_persists_completed_realtime_run(self) -> None:
        run = self.controller.run_case(
            self.case(),
            config={"network_profile_id": "wifi-baseline-v1", "latency_threshold_ms": 100},
        )
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["origin"], "xmax_realtime")
        self.assertEqual(run["metrics"]["input_method"], "connectMedia")
        self.assertEqual(run["metrics"]["single_round"], True)
        self.assertEqual(run["metrics"]["audio"]["publish"], True)
        self.assertEqual(run["metrics"]["audio"]["subscribe"], True)
        self.assertEqual(run["metrics"]["audio"]["contract_status"], "available")
        self.assertEqual(run["metrics"]["interaction_event_count"], 1)
        self.assertEqual(run["metrics"]["first_output_change_ms"], 80.0)
        self.assertEqual(run["metrics"]["interaction_latency_p50_ms"], 80.0)
        self.assertEqual(run["metrics"]["interaction_latency_p95_ms"], 80.0)
        self.assertEqual(run["metrics"]["interaction_latency_p99_ms"], 80.0)
        self.assertEqual(run["metrics"]["interaction_latency_jitter_ms"], 0.0)
        self.assertEqual(run["metrics"]["latency_threshold_exceed_ratio"], 0.0)
        self.assertEqual(run["metrics"]["network_profile_id"], "wifi-baseline-v1")
        self.assertEqual(run["metrics"]["duplicate_frame_ratio"], 0.0)
        self.assertEqual(run["metrics"]["freeze_duration_ms"], 0.0)
        self.assertEqual(run["metrics"]["first_valid_result_ms"], 200.0)
        self.assertIsNotNone(run["raw_events_uri"])
        events = self.repository.get_event_log(run["run_id"])
        self.assertEqual(events[0]["event"], "run_created")

    def test_static_touch_fake_persists_the_actual_capture_evidence(self) -> None:
        case = self.case(
            operation_recipe_version="0.3.0",
            api_asset_bindings={
                "input_method": "connectMedia",
                "input_media_role": "feed_capture",
                "capture_frame_policy": "seeded_random_safe_window_v1",
                "ref_image_role": "none",
                "interaction_profile_id": "pointer-track-30fps-v2",
            },
        )
        run = self.controller.run_case(case)
        capture = run["metrics"]["input_capture"]
        self.assertEqual(run["metrics"]["input_media_role"], "feed_capture")
        self.assertFalse(run["metrics"]["audio"]["publish"])
        self.assertEqual(capture["source_asset_id"], "feed-a")
        self.assertTrue(self.artifacts.resolve(capture["uri"]).is_file())

    def test_static_touch_rejects_harness_output_without_capture_evidence(self) -> None:
        clock = self.clock

        class MissingCaptureHarness:
            def run_case(self, case, config):
                return FakeRealtimeHarness(clock=clock).run_case(case, config)

        controller = RealtimeController(
            self.repository,
            self.artifacts,
            harness=MissingCaptureHarness(),
            clock=self.clock,
        )
        with self.assertRaises(ContractError):
            controller.run_case(
                self.case(
                    api_asset_bindings={
                        "input_method": "connectMedia",
                        "input_media_role": "feed_capture",
                        "capture_frame_policy": "seeded_random_safe_window_v1",
                        "ref_image_role": "none",
                        "interaction_profile_id": "pointer-track-30fps-v2",
                    }
                )
            )

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
        self.assertEqual(run["metrics"]["audio"]["subscribe_requested"], True)
        self.assertEqual(run["metrics"]["audio"]["remote_track_count"], 1)

    def test_missing_remote_audio_track_is_persisted_as_sdk_unavailable(self) -> None:
        run = self.controller.run_case(
            self.case(), config={"simulate_remote_audio": False}
        )
        self.assertFalse(run["metrics"]["audio"]["subscribe"])
        self.assertEqual(run["metrics"]["audio"]["remote_track_count"], 0)
        self.assertEqual(
            run["metrics"]["audio"]["contract_status"],
            "not_provided_by_realtime_sdk",
        )

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

    def test_network_rejection_retries_then_only_qualified_run_can_score(self) -> None:
        harness = FakeRealtimeHarness(clock=self.clock, artifacts=self.artifacts)
        controller = RealtimeController(
            self.repository,
            self.artifacts,
            harness=harness,
            clock=self.clock,
            sleeper=lambda _seconds: None,
        )
        run = controller.run_case(
            self.case(),
            config={
                "network_profile_id": "common-tun-webrtc-v1",
                "network_profile": self.network_profile(),
                "max_network_retries": 3,
                "fake_network_attempt_statuses": [
                    "rejected_preflight",
                    "rejected_runtime",
                    "qualified",
                ],
            },
        )
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["metrics"]["network_qualification"]["status"], "qualified")
        self.assertEqual(run["metrics"]["network_retry_count"], 2)
        attempts = sorted(
            self.repository.list_runs(case_id=self.case()["case_id"]),
            key=lambda item: item["metrics"]["network_attempt_index"],
        )
        self.assertEqual([item["status"] for item in attempts], ["cancelled", "cancelled", "completed"])
        self.assertEqual(len(run["metrics"]["network_attempt_run_ids"]), 3)
        for rejected in attempts[:2]:
            self.assertEqual(
                rejected["metrics"]["network_error"]["code"],
                "xmax.network_unqualified",
            )
            self.assertIsNotNone(rejected["raw_events_uri"])

    def test_network_retry_exhaustion_is_cancelled_and_not_completed(self) -> None:
        harness = FakeRealtimeHarness(clock=self.clock, artifacts=self.artifacts)
        controller = RealtimeController(
            self.repository,
            self.artifacts,
            harness=harness,
            clock=self.clock,
            sleeper=lambda _seconds: None,
        )
        run = controller.run_case(
            self.case(),
            config={
                "network_profile_id": "common-tun-webrtc-v1",
                "network_profile": self.network_profile(),
                "max_network_retries": 3,
                "fake_network_attempt_statuses": ["unverified"],
            },
        )
        self.assertEqual(run["status"], "cancelled")
        self.assertEqual(run["metrics"]["network_attempt_index"], 4)
        self.assertEqual(len(self.repository.list_runs(case_id=self.case()["case_id"])), 4)
        self.assertFalse(run["metrics"]["network_error"]["retryable"])

    def test_network_retry_count_is_bounded_to_five(self) -> None:
        controller = RealtimeController(
            self.repository,
            self.artifacts,
            harness=FakeRealtimeHarness(clock=self.clock),
            clock=self.clock,
            sleeper=lambda _seconds: None,
        )
        with self.assertRaises(ContractError):
            controller.run_case(
                self.case(),
                config={
                    "network_profile": self.network_profile(),
                    "max_network_retries": 6,
                },
            )


if __name__ == "__main__":
    unittest.main()
