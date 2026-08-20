"""Versioned realtime interaction profile expansion."""

from __future__ import annotations

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
    ) -> dict[str, Any]:
        if not profile_id:
            return {"profile": None, "tracks": []}
        profile = self._profiles.get(profile_id)
        if profile is None:
            raise ContractError(f"unknown realtime interaction profile: {profile_id}")
        if profile.get("event_kind") != "pointer_tracks":
            return {"profile": profile, "tracks": []}
        fps = float(profile.get("sample_fps", 30))
        tracks = []
        for segment in profile.get("segments", []):
            frame_count = max(1, round(float(segment["duration_ms"]) * fps / 1000))
            start = segment["from"]
            end = segment["to"]
            fingers = int(segment.get("fingers", 1))
            for index in range(frame_count + 1):
                ratio = index / frame_count
                x = round((start[0] + (end[0] - start[0]) * ratio) * width)
                y = round((start[1] + (end[1] - start[1]) * ratio) * height)
                points = [[x, y]]
                for finger in range(1, fingers):
                    points.append([min(width - 1, x + finger * 12), y])
                tracks.append(
                    {
                        "at_ms": round(float(segment["start_ms"]) + index * 1000 / fps, 3),
                        "points": points,
                    }
                )
        return {"profile": profile, "tracks": tracks}
