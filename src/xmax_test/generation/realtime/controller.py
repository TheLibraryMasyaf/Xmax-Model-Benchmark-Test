"""Realtime generation controller.

Orchestrates the browser harness (TypeScript) or an offline fake harness,
collects per-frame/event/RTC data, and persists a unified GenerationRun.
"""

from __future__ import annotations

import uuid
from typing import Any

from ...errors import ContractError
from ...hashing import content_hash, file_sha256
from ...time import utc_now


class FakeRealtimeHarness:
    """Python mirror of the TS harness for offline tests.

    Simulates: connect -> callbacks with timestamps -> single round for a fixed
    Feed (no auto-loop pollution) -> 30 FPS track frames mapped to the
    streamSetting content resolution -> explicit audio publish/subscribe ->
    optional disconnect/reconnect.
    """

    def __init__(self, clock: Any = None, fps: int = 30) -> None:
        self._clock = clock
        self._fps = fps

    def run_case(self, case: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        bindings = case.get("api_asset_bindings", {})
        input_method = bindings.get("input_method", "connectMedia")
        ref_image_role = bindings.get("ref_image_role", "none")
        interaction_profile = bindings.get("interaction_profile_id")
        content_width = config.get("content_width", 1280)
        content_height = config.get("content_height", 720)
        dom_width = config.get("dom_width", 640)
        dom_height = config.get("dom_height", 360)
        duration_s = config.get("duration_s", 3.0)

        session_uid = f"session-{uuid.uuid4().hex[:10]}"
        callbacks = [
            {
                "callback": "connect_call",
                "tsMonotonicMs": 0.0,
                "tsWallMs": self._wall(0.0),
            },
            {
                "callback": "connect_completed",
                "tsMonotonicMs": 120.0,
                "tsWallMs": self._wall(120.0),
                "sessionUid": session_uid,
            },
            {
                "callback": "onRemoteStream",
                "tsMonotonicMs": 180.0,
                "tsWallMs": self._wall(180.0),
            },
            {
                "callback": "onStateChange",
                "tsMonotonicMs": 200.0,
                "tsWallMs": self._wall(200.0),
                "state": "running",
            },
            {
                "callback": "onRoomEvent",
                "tsMonotonicMs": 210.0,
                "tsWallMs": self._wall(210.0),
                "event": "video_started",
            },
        ]

        frames: list[dict[str, Any]] = []
        frame_count = int(duration_s * self._fps)
        for index in range(frame_count):
            frames.append(
                {
                    "stream": "output",
                    "mediaTimeMs": index * (1000 / self._fps),
                    "arrivalTimeMs": 200.0 + index * (1000 / self._fps),
                    "featureHash": f"hash-{index % 7}",
                    "width": content_width,
                    "height": content_height,
                }
            )

        events: list[dict[str, Any]] = [
            {
                "event": "task_start",
                "plannedMs": 0.0,
                "executedMs": 0.0,
                "payload": {"round": 1},
            },
        ]
        # 30 FPS track frames mapped from DOM to content coordinates.
        for index in range(int(duration_s * 30)):
            screen = [[index % 100, 50]]
            content_x = round((screen[0][0] / max(1, dom_width)) * content_width)
            content_y = round((screen[0][1] / max(1, dom_height)) * content_height)
            events.append(
                {
                    "event": "tracks_frame",
                    "plannedMs": index * (1000 / 30),
                    "executedMs": index * (1000 / 30),
                    "screenCoords": screen,
                    "contentCoords": [[content_x, content_y]],
                    "sendResult": "sent",
                    "firstOutputChangeMs": None,
                }
            )
        events.append(
            {
                "event": "task_stop",
                "plannedMs": duration_s * 1000,
                "executedMs": duration_s * 1000,
                "payload": {"round": 1},
            }
        )

        rtc_log = [
            {
                "tsMonotonicMs": 500.0,
                "tsWallMs": self._wall(500.0),
                "resolution": {"width": content_width, "height": content_height},
                "bitrateBps": 1_500_000,
                "packetLossRatio": 0.001,
                "rttMs": 45.0,
            }
        ]

        state_changes = [
            {"state": "idle", "tsMonotonicMs": 0.0},
            {"state": "running", "tsMonotonicMs": 200.0},
        ]
        if config.get("simulate_disconnect"):
            state_changes.append({"state": "disconnected", "tsMonotonicMs": 1000.0})
            if config.get("simulate_reconnect"):
                state_changes.append({"state": "running", "tsMonotonicMs": 2500.0})
                callbacks.append(
                    {
                        "callback": "onRoomEvent",
                        "tsMonotonicMs": 2600.0,
                        "tsWallMs": self._wall(2600.0),
                        "event": "video_started",
                    }
                )

        audio = {"publish": True, "subscribe": True}
        return {
            "session_uid": session_uid,
            "input_method": input_method,
            "ref_image_role": ref_image_role,
            "interaction_profile_id": interaction_profile,
            "callbacks": callbacks,
            "frames": frames,
            "events": events,
            "rtc_log": rtc_log,
            "state_changes": state_changes,
            "audio": audio,
            "single_round": True,
            "stream_setting": {"width": content_width, "height": content_height},
            "metrics": self._metrics(callbacks, frames, events, duration_s),
        }

    def _wall(self, monotonic_ms: float) -> float:
        if self._clock is not None:
            return 0.0
        return monotonic_ms

    @staticmethod
    def _metrics(
        callbacks: list[dict[str, Any]],
        frames: list[dict[str, Any]],
        events: list[dict[str, Any]],
        duration_s: float,
    ) -> dict[str, Any]:
        connect_done = next((c for c in callbacks if c["callback"] == "connect_completed"), None)
        first_frame = next((f for f in frames if f["stream"] == "output"), None)
        return {
            "connect_ms": connect_done.get("tsMonotonicMs", 0) if connect_done else None,
            "first_frame_ms": first_frame.get("arrivalTimeMs") if first_frame else None,
            "fps": round(len(frames) / max(1.0, duration_s), 2),
            "frames_captured": len(frames),
            "events_recorded": len(events),
            "dropped_frames": 0,
            "rtt_ms": 45.0,
            "session_duration_s": duration_s,
            "fps_window_cv": 0.0,
            "track_send_success_ratio": 1.0,
        }


class RealtimeController:
    mode = "realtime"

    def __init__(
        self,
        repository: Any,
        artifacts: Any,
        harness: Any = None,
        *,
        model_id: str = "x2.0",
        run_batch_id: str = "run-batch-realtime",
        validator: Any = None,
        clock: Any = None,
    ) -> None:
        self._repository = repository
        self._artifacts = artifacts
        self._harness = harness or FakeRealtimeHarness(clock=clock)
        self.model_id = model_id
        self._run_batch_id = run_batch_id
        self._clock = clock
        self._validator = validator

    def run_case(
        self, case: dict[str, Any], config: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        config = config or {}
        bindings = case.get("api_asset_bindings", {})
        input_method = bindings.get("input_method")
        if input_method not in {"connectMedia", "connectCamera", "connect"}:
            raise ContractError(
                f"case {case['case_id']} has invalid realtime input_method: {input_method!r}"
            )
        result = self._harness.run_case(case, config)
        run_id = f"run-{uuid.uuid4().hex[:16]}"
        metrics = {
            **result.get("metrics", {}),
            "input_method": input_method,
            "interaction_profile_id": bindings.get("interaction_profile_id"),
            "audio": result.get("audio"),
            "single_round": result.get("single_round", True),
            "session_uid": result.get("session_uid"),
        }
        events_uri = self._save_artifacts(case, run_id, result)
        result_asset_id = self._register_recording(case, result)
        run = {
            "run_id": run_id,
            "run_batch_id": self._run_batch_id,
            "case_id": case["case_id"],
            "case_number": case["case_number"],
            "status": "completed",
            "model_id": case.get("model_id") or self.model_id,
            "mode": "realtime",
            "origin": "xmax_realtime",
            "provenance": {
                "source_type": "xmax_realtime",
                "source_locator": "realtime-harness",
                "source_hash": content_hash(
                    {
                        "case": case["case_id"],
                        "harness": type(self._harness).__name__,
                        "input_method": input_method,
                    }
                ),
            },
            "edited_video_asset_id": case.get("edited_video_asset_id"),
            "expected_audio_source_asset_id": case.get("expected_audio_source_asset_id"),
            "metrics": metrics,
            "raw_events_uri": events_uri,
            "result_asset_id": result_asset_id,
        }
        self._repository.create_run(run)
        return self._repository.get_run(run_id)

    def _register_recording(self, case: dict[str, Any], result: dict[str, Any]) -> str | None:
        uri = result.get("recording_uri")
        if not uri:
            return None
        path = self._artifacts.resolve(uri)
        sha256 = file_sha256(path)
        media = (
            self._validator.validate(path, "video_result")
            if self._validator
            else {
                "duration_s": result.get("metrics", {}).get("duration_s") or 1.0,
                "width": result.get("stream_setting", {}).get("width"),
                "height": result.get("stream_setting", {}).get("height"),
                "has_audio": result.get("audio", {}).get("subscribe", False),
            }
        )
        asset_id = f"asset_{sha256[:16]}"
        self._repository.upsert_asset(
            {
                "asset_id": asset_id,
                "kind": "result_video",
                "uri": uri,
                "sha256": sha256,
                "bytes": path.stat().st_size,
                "mime_type": "video/webm",
                "source": {"source_id": "xmax_realtime", "case_id": case["case_id"]},
                "status": "ready",
                "media": media,
                "metadata": {"case_id": case["case_id"]},
                "created_at": utc_now(),
            }
        )
        return asset_id

    def _save_artifacts(
        self, case: dict[str, Any], run_id: str, result: dict[str, Any]
    ) -> str | None:
        import json as _json

        payload = {
            "session_uid": result.get("session_uid"),
            "callbacks": result.get("callbacks", []),
            "frames": result.get("frames", []),
            "events": result.get("events", []),
            "rtc_log": result.get("rtc_log", []),
            "state_changes": result.get("state_changes", []),
            "browser_log": result.get("browser_log", []),
            "audio": result.get("audio"),
            "single_round": result.get("single_round", True),
            "stream_setting": result.get("stream_setting"),
        }
        stored = self._artifacts.put_bytes(
            "runs",
            f"{run_id}/realtime.json",
            _json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        return stored["uri"]
