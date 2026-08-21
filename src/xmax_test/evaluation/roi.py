"""ROI extraction metadata.

ROIs are derived evidence: region + source frame + confidence. They never
replace runtime facts (FPS/latency/network) measured from API/RTC timestamps.
"""

from __future__ import annotations

from typing import Any


class RoiExtractor:
    def __init__(self, extractor: Any | None = None) -> None:
        self._extractor = extractor

    def extract(
        self, frame: dict[str, Any], query: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Return ROIs for one frame.

        A real extractor (detector/tracker) can be injected; the default
        returns an empty region list when no extractor is available rather
        than fabricating evidence.
        """

        if self._extractor is None:
            return []
        return self._extractor.extract(frame, query or {})


class FakeRoiExtractor:
    """Deterministic ROI extractor for offline tests."""

    def __init__(self, rois: list[dict[str, Any]] | None = None) -> None:
        self._rois = rois or [
            {
                "region": [10, 20, 100, 200],
                "track_id": "subject-1",
                "confidence": 0.9,
                "frame_index": 0,
            }
        ]

    def extract(
        self, frame: dict[str, Any], query: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        return [dict(item, frame_index=frame.get("index", 0)) for item in self._rois]
