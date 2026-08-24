"""Versioned realtime interaction profile expansion."""

from __future__ import annotations

import hashlib
import math
import random
from typing import Any

from ...errors import ContractError


class InteractionProfileResolver:
    def __init__(self, pack: dict[str, Any]) -> None:
        self._pack = pack
        self._profiles = {item["profile_id"]: item for item in pack.get("profiles", [])}

    def expand(
        self,
        profile_id: str | None,
        *,
        width: int,
        height: int,
        seed_key: str | None = None,
        duration_ms: float = 3000,
    ) -> dict[str, Any]:
        if not profile_id:
            return {"profile": None, "tracks": []}
        profile = self._profiles.get(profile_id)
        if profile is None:
            raise ContractError(f"unknown realtime interaction profile: {profile_id}")
        if profile.get("event_kind") != "pointer_tracks":
            return {"profile": profile, "tracks": []}
        fps = float(profile.get("sample_fps", 30))
        segments = list(profile.get("segments", []))
        randomization = profile.get("randomization")
        if randomization:
            segments = self._randomized_segments(
                profile,
                randomization,
                seed_key=seed_key or "default",
                duration_ms=duration_ms,
            )
        tracks = []
        for segment_index, segment in enumerate(segments):
            frame_count = max(1, round(float(segment["duration_ms"]) * fps / 1000))
            start = segment["from"]
            end = segment["to"]
            fingers = int(segment.get("fingers", 1))
            curve = float(segment.get("curve", 0.0))
            jitter = float(segment.get("jitter", 0.0))
            easing = str(segment.get("easing", "linear"))
            frame_rng = random.Random(int(segment.get("jitter_seed", 0)))
            for index in range(frame_count + 1):
                ratio = index / frame_count
                eased = self._ease(ratio, easing)
                normalized_x, normalized_y = self._curve_point(start, end, eased, curve)
                if jitter and index not in {0, frame_count}:
                    normalized_x += frame_rng.uniform(-jitter, jitter)
                    normalized_y += frame_rng.uniform(-jitter, jitter)
                normalized_x = min(1.0, max(0.0, normalized_x))
                normalized_y = min(1.0, max(0.0, normalized_y))
                x = round(normalized_x * width)
                y = round(normalized_y * height)
                points = [[x, y]]
                for finger in range(1, fingers):
                    points.append([min(width - 1, x + finger * 12), y])
                tracks.append(
                    {
                        "at_ms": round(float(segment["start_ms"]) + index * 1000 / fps, 3),
                        "points": points,
                        "swipe_id": f"swipe-{segment_index + 1}",
                        "phase": (
                            "start"
                            if index == 0
                            else "end" if index == frame_count else "move"
                        ),
                    }
                )
        return {"profile": profile, "tracks": tracks}

    @staticmethod
    def _ease(ratio: float, easing: str) -> float:
        if easing == "ease_out":
            return 1 - (1 - ratio) ** 3
        if easing == "ease_in":
            return ratio**3
        if easing == "ease_in_out":
            return ratio * ratio * (3 - 2 * ratio)
        return ratio

    @staticmethod
    def _curve_point(
        start: list[float],
        end: list[float],
        ratio: float,
        curve: float,
    ) -> tuple[float, float]:
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        distance = math.hypot(dx, dy)
        if not distance or not curve:
            return start[0] + dx * ratio, start[1] + dy * ratio
        control_x = (start[0] + end[0]) / 2 - (dy / distance) * curve
        control_y = (start[1] + end[1]) / 2 + (dx / distance) * curve
        inverse = 1 - ratio
        return (
            inverse * inverse * start[0]
            + 2 * inverse * ratio * control_x
            + ratio * ratio * end[0],
            inverse * inverse * start[1]
            + 2 * inverse * ratio * control_y
            + ratio * ratio * end[1],
        )

    def _randomized_segments(
        self,
        profile: dict[str, Any],
        settings: dict[str, Any],
        *,
        seed_key: str,
        duration_ms: float,
    ) -> list[dict[str, Any]]:
        if duration_ms <= 0:
            raise ContractError("randomized interaction duration_ms must be positive")
        seed_material = (
            f"{self._pack.get('profile_pack_id', '')}:"
            f"{self._pack.get('version', '')}:"
            f"{profile.get('profile_id', '')}:{profile.get('version', '')}:{seed_key}"
        )
        seed = int.from_bytes(hashlib.sha256(seed_material.encode("utf-8")).digest()[:8], "big")
        rng = random.Random(seed)

        count_min, count_max = self._ordered_range(settings["swipe_count"], "swipe_count")
        count_min, count_max = int(count_min), int(count_max)
        swipe_count = rng.randint(count_min, count_max)
        duration_range = self._ordered_range(settings["duration_ms"], "duration_ms")
        gap_range = self._ordered_range(settings["gap_ms"], "gap_ms")
        start_range = self._ordered_range(settings["start_delay_ms"], "start_delay_ms")
        raw_durations = [rng.uniform(*duration_range) for _ in range(swipe_count)]
        raw_gaps = [rng.uniform(*gap_range) for _ in range(max(0, swipe_count - 1))]
        start_ms = rng.uniform(*start_range)
        end_padding_ms = float(settings.get("end_padding_ms", 150))
        available_ms = max(1.0, duration_ms - start_ms - end_padding_ms)
        raw_total = sum(raw_durations) + sum(raw_gaps)
        scale = min(1.0, available_ms / max(1.0, raw_total))
        durations = [value * scale for value in raw_durations]
        gaps = [value * scale for value in raw_gaps]

        margin = float(settings["edge_margin"])
        distance_min, distance_max = self._ordered_range(settings["distance"], "distance")
        curvature_min, curvature_max = self._ordered_range(
            settings["curvature"], "curvature"
        )
        if distance_max > 1 - 2 * margin:
            raise ContractError("randomized interaction distance exceeds the safe canvas span")
        jitter = float(settings.get("jitter", 0.0))
        easings = list(settings.get("easing", ["ease_in_out"]))
        directions = list(range(8))
        rng.shuffle(directions)
        while len(directions) < swipe_count:
            extra = list(range(8))
            rng.shuffle(extra)
            directions.extend(extra)

        segments: list[dict[str, Any]] = []
        cursor = start_ms
        for index in range(swipe_count):
            angle = directions[index] * (math.pi / 4) + rng.uniform(-math.pi / 10, math.pi / 10)
            distance = rng.uniform(distance_min, distance_max)
            dx = math.cos(angle) * distance
            dy = math.sin(angle) * distance
            start_x = rng.uniform(max(margin, margin - dx), min(1 - margin, 1 - margin - dx))
            start_y = rng.uniform(max(margin, margin - dy), min(1 - margin, 1 - margin - dy))
            segments.append(
                {
                    "start_ms": round(cursor, 3),
                    "duration_ms": round(durations[index], 3),
                    "from": [start_x, start_y],
                    "to": [start_x + dx, start_y + dy],
                    "fingers": 1,
                    "curve": rng.uniform(curvature_min, curvature_max),
                    "jitter": jitter,
                    "jitter_seed": rng.getrandbits(64),
                    "easing": rng.choice(easings),
                }
            )
            cursor += durations[index]
            if index < len(gaps):
                cursor += gaps[index]
        return segments

    @staticmethod
    def _ordered_range(values: list[Any], name: str) -> tuple[float, float]:
        low, high = (float(value) for value in values)
        if low > high:
            raise ContractError(f"randomized interaction {name} range must be ascending")
        return low, high
