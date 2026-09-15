"""Realtime generation controller.

Orchestrates the browser harness (TypeScript) or an offline fake harness,
collects per-frame/event/RTC data, and persists a unified GenerationRun.
"""

from __future__ import annotations

import uuid
import math
import statistics
import time
from typing import Any

from ...errors import ContractError
from ...hashing import content_hash, file_sha256
from ...planning.builder import generation_signature
from ...time import utc_now
from .network import NetworkProfileResolver, evaluate_network_qualification


class FakeRealtimeHarness:
    """Python mirror of the TS harness for offline tests.

    Simulates: connect -> callbacks with timestamps -> single round for a fixed
    Feed (no auto-loop pollution) -> 30 FPS track frames mapped to the
    streamSetting content resolution -> explicit audio publish/subscribe ->
    optional disconnect/reconnect.
    """

    def __init__(self, clock: Any = None, fps: int = 30, artifacts: Any = None) -> None:
        self._clock = clock
        self._fps = fps
        self._artifacts = artifacts
        self._attempt_count = 0

    def run_case(self, case: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        self._attempt_count += 1
        bindings = case.get("api_asset_bindings", {})
        input_method = bindings.get("input_method", "connectMedia")
        ref_image_role = bindings.get("ref_image_role", "none")
        interaction_profile = bindings.get("interaction_profile_id")
        input_media_role = bindings.get("input_media_role", "feed_video")
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
                    "responseProbe": index == 0,
                    "firstOutputChangeMs": 80.0 if index == 0 else None,
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

        rtc_log = self._rtc_log(config)

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

        remote_audio_present = bool(config.get("simulate_remote_audio", True))
        input_audio_present = input_media_role != "feed_capture"
        audio = {
            "publish_requested": True,
            "subscribe_requested": True,
            "publish": input_audio_present,
            "subscribe": remote_audio_present,
            "input_track_count": 1 if input_audio_present else 0,
            "remote_track_count": 1 if remote_audio_present else 0,
        }
        result = {
            "session_uid": session_uid,
            "input_method": input_method,
            "ref_image_role": ref_image_role,
            "interaction_profile_id": interaction_profile,
            "input_media_role": input_media_role,
            "callbacks": callbacks,
            "frames": frames,
            "events": events,
            "rtc_log": rtc_log,
            "network_environment": config.get(
                "network_environment",
                {"tun_active": True, "active_tun_interfaces": ["utun0"]},
            ),
            "state_changes": state_changes,
            "audio": audio,
            "single_round": True,
            "stream_setting": {"width": content_width, "height": content_height},
            "metrics": self._metrics(callbacks, frames, events, duration_s),
        }
        if input_media_role == "feed_capture" and self._artifacts is not None:
            stored = self._artifacts.put_bytes(
                "captures",
                f"realtime/{case['case_id']}/fake-feed-capture.jpg",
                b"\xff\xd8\xff-fake-realtime-feed-capture",
            )
            result["input_capture"] = {
                **stored,
                "source_asset_id": case.get("feed_asset_id"),
                "source_sha256": "fake-source-sha256",
                "timestamp_s": 1.5,
                "capture_policy": bindings.get("capture_frame_policy"),
                "producer_version": "fake-realtime-feed-capture-v1",
            }
        return result

    def _rtc_log(self, config: dict[str, Any]) -> list[dict[str, Any]]:
        if not config.get("network_profile"):
            return [
                {
                    "tsMonotonicMs": 500.0,
                    "tsWallMs": self._wall(500.0),
                    "resolution": {"width": 1280, "height": 720},
                    "bitrateBps": 1_500_000,
                    "packetLossRatio": 0.001,
                    "rttMs": 45.0,
                }
            ]
        statuses = config.get("fake_network_attempt_statuses", ["qualified"])
        status = statuses[min(self._attempt_count - 1, len(statuses) - 1)]
        if status == "unverified":
            return []
        samples: list[dict[str, Any]] = []
        for phase, count, start in (("preflight", 5, 500.0), ("runtime", 4, 6000.0)):
            for index in range(count):
                bad = (status == "rejected_preflight" and phase == "preflight") or (
                    status == "rejected_runtime" and phase == "runtime"
                )
                rtt = 0.2 if bad else 0.035
                samples.append(
                    {
                        "phase": phase,
                        "tsMonotonicMs": start + index * 1000,
                        "tsWallMs": self._wall(start + index * 1000),
                        "peerIndex": 0,
                        "entries": [
                            {
                                "id": "candidate-pair-1",
                                "type": "candidate-pair",
                                "state": "succeeded",
                                "nominated": True,
                                "currentRoundTripTime": rtt,
                                "availableOutgoingBitrate": 2_000_000,
                                "bytesSent": 1000 + index * 100,
                                "bytesReceived": 2000 + index * 100,
                            },
                            {
                                "id": "in-video",
                                "type": "inbound-rtp",
                                "kind": "video",
                                "packetsLost": 0,
                                "packetsReceived": 100 + index * 20,
                                "jitter": 0.01,
                            },
                            {
                                "id": "out-video",
                                "type": "outbound-rtp",
                                "kind": "video",
                                "packetsSent": 100 + index * 20,
                            },
                            {
                                "id": "remote-in-video",
                                "type": "remote-inbound-rtp",
                                "kind": "video",
                                "localId": "out-video",
                                "packetsLost": 0,
                            },
                        ],
                    }
                )
        return samples

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
        metrics = {
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
        return _derive_realtime_metrics(callbacks, frames, events, metrics)


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
        sleeper: Any = time.sleep,
    ) -> None:
        self._repository = repository
        self._artifacts = artifacts
        self._harness = harness or FakeRealtimeHarness(clock=clock, artifacts=artifacts)
        self.model_id = model_id
        self._run_batch_id = run_batch_id
        self._clock = clock
        self._validator = validator
        self._sleeper = sleeper

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
        profile = config.get("network_profile")
        maximum_retries = 0
        if profile:
            resolver = NetworkProfileResolver({"profiles": [profile]})
            maximum_retries = resolver.retry_count(
                profile, config.get("max_network_retries")
            )
        attempt_run_ids: list[str] = []
        for attempt_index in range(1, maximum_retries + 2):
            result = self._harness.run_case(case, config)
            if profile:
                qualification = evaluate_network_qualification(
                    rtc_log=result.get("rtc_log", []),
                    state_changes=result.get("state_changes", []),
                    environment=result.get("network_environment"),
                    profile=profile,
                )
            else:
                qualification = {
                    "required": False,
                    "status": "not_required",
                    "model_output_metrics_used": False,
                }
            run_id = f"run-{uuid.uuid4().hex[:16]}"
            run = self._persist_attempt(
                case,
                config,
                result,
                input_method=input_method,
                run_id=run_id,
                qualification=qualification,
                attempt_index=attempt_index,
                maximum_retries=maximum_retries,
                attempt_run_ids=[*attempt_run_ids, run_id],
            )
            attempt_run_ids.append(run_id)
            if qualification["status"] in {"qualified", "not_required"}:
                return run
            if attempt_index <= maximum_retries:
                cooldown = float(profile.get("retry", {}).get("cooldown_s", 0))
                if cooldown:
                    self._sleeper(cooldown)
        return run

    def _persist_attempt(
        self,
        case: dict[str, Any],
        config: dict[str, Any],
        result: dict[str, Any],
        *,
        input_method: str,
        run_id: str,
        qualification: dict[str, Any],
        attempt_index: int,
        maximum_retries: int,
        attempt_run_ids: list[str],
    ) -> dict[str, Any]:
        bindings = case.get("api_asset_bindings", {})
        profile = config.get("network_profile")
        result["network_qualification"] = qualification
        input_capture = result.get("input_capture")
        if bindings.get("input_media_role") == "feed_capture":
            required_capture_fields = {
                "uri",
                "sha256",
                "source_asset_id",
                "source_sha256",
                "timestamp_s",
                "capture_policy",
                "producer_version",
            }
            missing = sorted(required_capture_fields - set(input_capture or {}))
            if missing:
                raise ContractError(
                    f"realtime Feed capture evidence missing fields: {', '.join(missing)}"
                )
            if input_capture["source_asset_id"] != case.get("feed_asset_id"):
                raise ContractError("realtime Feed capture source does not match the Case Feed")
            if input_capture["capture_policy"] != bindings.get("capture_frame_policy"):
                raise ContractError("realtime Feed capture policy does not match the Case binding")
            self._artifacts.verify(input_capture["uri"], input_capture["sha256"])
        metrics = {
            **result.get("metrics", {}),
            "feed_preprocessing": result.get("feed_preprocessing"),
            "input_method": input_method,
            "input_media_role": bindings.get("input_media_role", "feed_video"),
            "interaction_profile_id": bindings.get("interaction_profile_id"),
            "audio": result.get("audio"),
            "single_round": result.get("single_round", True),
            "session_uid": result.get("session_uid"),
            "generation_signature": case.get("generation_signature")
            or generation_signature(case),
            "network_qualification": qualification,
            "network_attempt_index": attempt_index,
            "max_network_retries": maximum_retries,
            "network_retry_count": attempt_index - 1,
            "network_attempt_run_ids": attempt_run_ids,
        }
        if config.get("network_profile_id"):
            metrics["network_profile_id"] = str(config["network_profile_id"])
            metrics["network_profile_version"] = str(profile.get("version")) if profile else None
            metrics["network_profile_hash"] = config.get("network_profile_hash")
        if isinstance(config.get("latency_threshold_ms"), (int, float)):
            metrics["latency_threshold_ms"] = float(config["latency_threshold_ms"])
        metrics = _derive_realtime_metrics(
            result.get("callbacks", []),
            result.get("frames", []),
            result.get("events", []),
            metrics,
        )
        if input_capture:
            metrics["input_capture"] = input_capture
        events_uri = self._save_artifacts(case, run_id, result)
        result_asset_id = self._register_recording(case, result)
        audio = dict(metrics.get("audio") or {})
        audio["publish_requested"] = audio.get("publish_requested", True)
        audio["subscribe_requested"] = audio.get("subscribe_requested", True)
        audio["remote_track_count"] = int(
            audio.get("remote_track_count") or (1 if audio.get("subscribe") else 0)
        )
        recording_has_audio = False
        if result_asset_id:
            recording_has_audio = bool(
                self._repository.get_asset(result_asset_id).get("media", {}).get("has_audio")
            )
        audio["recording_has_audio"] = recording_has_audio
        audio["contract_status"] = (
            "available"
            if audio.get("subscribe")
            else "not_provided_by_realtime_sdk"
        )
        metrics["audio"] = audio
        if qualification["status"] not in {"qualified", "not_required"}:
            metrics["network_error"] = {
                "code": "xmax.network_unqualified",
                "message": (
                    f"network attempt {attempt_index}/{maximum_retries + 1} "
                    f"was {qualification['status']}"
                ),
                "retryable": attempt_index <= maximum_retries,
                "reasons": qualification.get("reasons", []),
                "evidence_gaps": qualification.get("evidence_gaps", []),
            }
        run = {
            "run_id": run_id,
            "run_batch_id": self._run_batch_id,
            "case_id": case["case_id"],
            "case_number": case["case_number"],
            "status": (
                "completed"
                if qualification["status"] in {"qualified", "not_required"}
                else "cancelled"
            ),
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
                        "input_media_role": bindings.get("input_media_role", "feed_video"),
                        "input_capture": result.get("input_capture"),
                        "network_profile_id": config.get("network_profile_id"),
                        "network_profile_version": (
                            profile.get("version") if profile else None
                        ),
                        "network_profile_hash": config.get("network_profile_hash"),
                    }
                ),
            },
            "edited_video_asset_id": case.get("edited_video_asset_id"),
            "expected_audio_source_asset_id": case.get("expected_audio_source_asset_id"),
            "metrics": metrics,
            "raw_events_uri": events_uri,
            "result_asset_id": result_asset_id,
        }
        if run["status"] != "completed":
            run["error"] = metrics["network_error"]
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
            "input_media_role": result.get("input_media_role"),
            "input_capture": result.get("input_capture"),
            "network_environment": result.get("network_environment"),
            "network_qualification": result.get("network_qualification"),
            "network_harness_decision": result.get("network_harness_decision"),
        }
        stored = self._artifacts.put_bytes(
            "runs",
            f"{run_id}/realtime.json",
            _json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        return stored["uri"]


def _derive_realtime_metrics(
    callbacks: list[dict[str, Any]],
    frames: list[dict[str, Any]],
    events: list[dict[str, Any]],
    existing: dict[str, Any],
) -> dict[str, Any]:
    """Derive auditable realtime facts from monotonic frame/event records.

    A response probe is one discrete interaction start.  Its
    ``firstOutputChangeMs`` is measured by the browser Harness from the sent
    input to the first materially changed output-frame hash.  This measures
    response speed only; R2 remains responsible for semantic correctness.
    """

    metrics = dict(existing)
    output = sorted(
        (
            item
            for item in frames
            if item.get("stream") == "output"
            and isinstance(item.get("arrivalTimeMs"), (int, float))
        ),
        key=lambda item: float(item["arrivalTimeMs"]),
    )
    hashed = [item for item in output if item.get("featureHash")]
    if len(hashed) >= 2:
        duplicate_count = sum(
            current.get("featureHash") == previous.get("featureHash")
            for previous, current in zip(hashed, hashed[1:], strict=False)
        )
        metrics.setdefault("duplicate_frame_ratio", duplicate_count / (len(hashed) - 1))
        longest_start = float(hashed[0]["arrivalTimeMs"])
        longest_ms = 0.0
        for previous, current in zip(hashed, hashed[1:], strict=False):
            if current.get("featureHash") != previous.get("featureHash"):
                longest_start = float(current["arrivalTimeMs"])
            longest_ms = max(longest_ms, float(current["arrivalTimeMs"]) - longest_start)
        metrics.setdefault("freeze_duration_ms", longest_ms)

    connect_call = next(
        (item for item in callbacks if item.get("callback") == "connect_call"), None
    )
    first_valid = next(
        (
            item
            for item in output
            if int(item.get("width") or 0) > 0 and int(item.get("height") or 0) > 0
        ),
        None,
    )
    if connect_call and first_valid:
        start = connect_call.get("tsMonotonicMs")
        if isinstance(start, (int, float)):
            metrics.setdefault(
                "first_valid_result_ms", float(first_valid["arrivalTimeMs"]) - float(start)
            )

    probes = [item for item in events if item.get("responseProbe") is True]
    latencies = [
        float(item["firstOutputChangeMs"])
        for item in probes
        if isinstance(item.get("firstOutputChangeMs"), (int, float))
    ]
    metrics["interaction_event_count"] = len(probes)
    metrics["interaction_latency_observed_count"] = len(latencies)
    if latencies:
        ordered = sorted(latencies)
        metrics.setdefault("first_output_change_ms", latencies[0])
        metrics["interaction_latency_p50_ms"] = _percentile(ordered, 0.5)
        metrics["interaction_latency_p95_ms"] = _percentile(ordered, 0.95)
        metrics["interaction_latency_p99_ms"] = _percentile(ordered, 0.99)
        metrics["interaction_latency_jitter_ms"] = (
            statistics.pstdev(latencies) if len(latencies) > 1 else 0.0
        )
        metrics["interaction_latency_slope_ms_per_event"] = _linear_slope(latencies)
        mean = statistics.mean(latencies)
        metrics["latency_window_cv"] = (
            statistics.pstdev(latencies) / mean if len(latencies) > 1 and mean else 0.0
        )
        metrics["pending_event_peak"] = _pending_peak(probes)
        threshold = metrics.get("latency_threshold_ms")
        if isinstance(threshold, (int, float)) and float(threshold) > 0:
            metrics["latency_threshold_exceed_ratio"] = sum(
                value > float(threshold) for value in latencies
            ) / len(latencies)
    return metrics


def _pending_peak(probes: list[dict[str, Any]]) -> int:
    boundaries: list[tuple[float, int]] = []
    for item in probes:
        start = item.get("executedMs")
        latency = item.get("firstOutputChangeMs")
        if not isinstance(start, (int, float)) or not isinstance(latency, (int, float)):
            continue
        boundaries.extend([(float(start), 1), (float(start) + float(latency), -1)])
    active = peak = 0
    for _, delta in sorted(boundaries, key=lambda item: (item[0], item[1])):
        active += delta
        peak = max(peak, active)
    return peak


def _linear_slope(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    center_x = (len(values) - 1) / 2
    center_y = statistics.mean(values)
    numerator = sum((index - center_x) * (value - center_y) for index, value in enumerate(values))
    denominator = sum((index - center_x) ** 2 for index in range(len(values)))
    return numerator / denominator if denominator else 0.0


def _percentile(values: list[float], fraction: float) -> float:
    if len(values) == 1:
        return values[0]
    position = (len(values) - 1) * fraction
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return values[lower]
    return values[lower] + (values[upper] - values[lower]) * (position - lower)
