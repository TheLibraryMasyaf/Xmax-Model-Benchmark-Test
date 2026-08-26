"""MLLM-backed classifier for assigning one of the frozen core scenarios."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..errors import ConfigError, ContractError


class ScenarioClassifier:
    def __init__(self, provider: Any, scenario_pack: dict[str, Any]) -> None:
        self._provider = provider
        self._scenarios = {
            item["scenario_id"]: item for item in scenario_pack.get("scenarios", [])
        }

    def classify(
        self,
        *,
        feed_video_path: str | Path,
        prompt_text: str,
        prompt_reference_path: str | Path | None = None,
        fallback_feed_frame_paths: list[str | Path] | None = None,
    ) -> dict[str, Any]:
        choices = [
            {
                "scenario_id": item["scenario_id"],
                "name": item["name"],
                "description": item.get("description", ""),
            }
            for item in self._scenarios.values()
        ]
        schema = {
            "type": "object",
            "required": ["scenario_id", "confidence", "rationale", "observed_facts"],
            "properties": {
                "scenario_id": {"type": "string", "enum": list(self._scenarios)},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "rationale": {"type": "string"},
                "observed_facts": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 5,
                },
            },
            "additionalProperties": False,
        }
        media_inputs: list[dict[str, Any]] = [
            {"role": "feed_video", "kind": "video", "path": str(feed_video_path)},
            {"role": "prompt_text", "kind": "text", "text": prompt_text},
        ]
        if prompt_reference_path is not None:
            path = Path(prompt_reference_path)
            kind = "video" if path.suffix.lower() in {".mp4", ".mov", ".webm"} else "image"
            media_inputs.append(
                {"role": "prompt_reference", "kind": kind, "path": str(path)}
            )
        prompt = (
            "你是XMAX测试数据集的场景分类器。同时观察Feed视频、Prompt文字和"
            "Prompt素材，只能选择一个最匹配的核心场景。优先根据Feed的拍摄方式、主体数量、"
            "主体类型、运动与环境复杂度分类，再用指令对象和目的消除歧义。不要根据原有标签猜测。\n"
            f"候选场景：{json.dumps(choices, ensure_ascii=False)}\n"
            f"Prompt文字：{prompt_text}"
        )
        fallback_used = False
        try:
            response = self._provider.complete_json(
                prompt=prompt,
                image_paths=[],
                output_schema=schema,
                media_inputs=media_inputs,
            )
        except ConfigError as exc:
            if "exceeds Base64 limit" not in str(exc) or not fallback_feed_frame_paths:
                raise
            fallback_used = True
            fallback_media = [
                {"role": "prompt_text", "kind": "text", "text": prompt_text}
            ]
            if prompt_reference_path is not None:
                path = Path(prompt_reference_path)
                if path.suffix.lower() not in {".mp4", ".mov", ".webm"}:
                    fallback_media.append(
                        {"role": "prompt_reference", "kind": "image", "path": str(path)}
                    )
            response = self._provider.complete_json(
                prompt=prompt + "\n补充图像按时间顺序均为Feed视频抽帧。",
                image_paths=[str(path) for path in fallback_feed_frame_paths],
                output_schema=schema,
                media_inputs=fallback_media,
            )
        payload = dict(response.payload)
        if payload.get("scenario_id") not in self._scenarios:
            raise ContractError(f"classifier returned unknown scenario: {payload.get('scenario_id')}")
        return {
            **payload,
            "provider_id": response.provider_id,
            "model": response.model,
            "usage": response.usage,
            "fallback_to_feed_frames": fallback_used,
        }
