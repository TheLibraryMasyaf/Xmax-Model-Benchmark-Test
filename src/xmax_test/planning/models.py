"""Planning domain models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class PromptBundle:
    """One prompt unit: text plus optional reference assets."""

    prompt_number: str
    prompt_text: str
    prompt_asset_ids: tuple[str, ...] = ()
    play_name: str | None = None
    scenario_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CaseCandidate:
    """One Feed x Prompt x recipe x mode x repeat combination."""

    feed_asset_id: str
    feed_number: str
    prompt: PromptBundle
    recipe_id: str
    recipe_version: str
    mode: str
    repeat_index: int
    model_id: str
    edited_video_asset_id: str
    expected_audio_source_asset_id: str
    api_bindings: dict[str, str] = field(default_factory=dict)
    generation_config: dict[str, Any] = field(default_factory=dict)
    skip_reason: str | None = None


@dataclass(frozen=True)
class PlanSnapshot:
    """Frozen, deterministic test plan."""

    plan_id: str
    plan_version: str
    plan_hash: str
    input_asset_batch_ids: tuple[str, ...]
    benchmark_version: str
    scenario_pack_version: str
    seed: int
    cases: tuple[dict[str, Any], ...]
    created_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "plan_version": self.plan_version,
            "plan_hash": self.plan_hash,
            "input_asset_batch_ids": list(self.input_asset_batch_ids),
            "benchmark_version": self.benchmark_version,
            "scenario_pack_version": self.scenario_pack_version,
            "seed": self.seed,
            "cases": [dict(case) for case in self.cases],
            "created_at": self.created_at,
            "metadata": self.metadata,
        }
