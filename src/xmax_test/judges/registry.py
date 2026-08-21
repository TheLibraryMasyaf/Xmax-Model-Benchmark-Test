"""In-memory versioned Judge registry."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RegisteredJudge:
    judge_id: str
    version: str
    plugin: Any
    manifest: dict[str, Any]


class JudgeRegistry:
    def __init__(self) -> None:
        self._judges: dict[tuple[str, str], RegisteredJudge] = {}

    def register(self, plugin: Any) -> RegisteredJudge:
        manifest = plugin.manifest()
        judge_id = manifest.get("judge_id")
        version = manifest.get("version")
        if not isinstance(judge_id, str) or not judge_id:
            raise ValueError("judge manifest requires judge_id")
        if not isinstance(version, str) or not version:
            raise ValueError("judge manifest requires version")
        key = (judge_id, version)
        if key in self._judges:
            raise ValueError(f"judge already registered: {judge_id}@{version}")
        item = RegisteredJudge(judge_id, version, plugin, manifest)
        self._judges[key] = item
        return item

    def get(self, judge_id: str, version: str) -> RegisteredJudge:
        try:
            return self._judges[(judge_id, version)]
        except KeyError as exc:
            raise KeyError(f"unknown judge: {judge_id}@{version}") from exc

    def for_dimension(self, dimension_id: str, mode: str) -> list[RegisteredJudge]:
        matches: list[RegisteredJudge] = []
        for item in self._judges.values():
            dimensions = item.manifest.get("supported_dimensions", [])
            modes = item.manifest.get("supported_modes", [])
            if dimension_id in dimensions and mode in modes:
                matches.append(item)
        return sorted(matches, key=lambda item: (item.judge_id, item.version))

    def manifests(self) -> list[dict[str, Any]]:
        return [
            dict(item.manifest)
            for item in sorted(
                self._judges.values(), key=lambda value: (value.judge_id, value.version)
            )
        ]
