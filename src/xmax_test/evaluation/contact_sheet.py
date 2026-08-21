"""Contact-sheet builders.

Contact sheets are visual evidence carriers only; they are never used to
derive runtime metrics (FPS, latency, device/network facts come from
API/RTC/system timestamps).
"""

from __future__ import annotations

import json
from typing import Any

from ..hashing import content_hash


class ContactSheetBuilder:
    def __init__(self, renderer: Any | None = None) -> None:
        self._renderer = renderer

    def build(
        self,
        *,
        run_id: str,
        frames: list[dict[str, Any]],
        kind: str,
        producer_version: str,
        parameters: dict[str, Any],
    ) -> dict[str, Any]:
        """Build one contact sheet artifact; returns its manifest.

        ``renderer`` (injected) draws the actual image; the default writes the
        frame manifest only, which is enough for evidence references.
        """

        sheet_id = f"sheet-{content_hash({'run': run_id, 'kind': kind, 'frames': [f.get('index') for f in frames]})[:12]}"
        manifest = {
            "sheet_id": sheet_id,
            "run_id": run_id,
            "kind": kind,
            "frames": [
                {
                    "index": frame.get("index"),
                    "timestamp_s": frame.get("timestamp_s"),
                    "uri": frame.get("uri"),
                }
                for frame in frames
            ],
            "producer_version": producer_version,
            "parameters": parameters,
        }
        if self._renderer is not None:
            manifest["image_uri"] = self._renderer.render(sheet_id, frames, parameters)
        return manifest


class FakeContactSheetRenderer:
    """Writes a placeholder sheet JSON into the artifact namespace."""

    def __init__(self, artifacts: Any) -> None:
        self._artifacts = artifacts

    def render(
        self, sheet_id: str, frames: list[dict[str, Any]], parameters: dict[str, Any]
    ) -> str:
        payload = json.dumps(
            {"sheet_id": sheet_id, "frames": len(frames), "parameters": parameters},
            ensure_ascii=False,
        ).encode("utf-8")
        stored = self._artifacts.put_bytes("preprocessing", f"{sheet_id}/sheet.json", payload)
        return stored["uri"]
