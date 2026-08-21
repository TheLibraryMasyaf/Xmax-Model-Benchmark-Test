"""Deterministic test-plan builder.

Same inputs, same number snapshot and same seed always produce the same
case_id/case_number. The builder freezes recipe ID/version, edited video,
expected audio source and API bindings; it never guesses API roles from file
extensions.
"""

from __future__ import annotations

import hashlib
import math
from typing import Any

from ..hashing import content_hash
from ..tasks import TaskAllocator
from ..time import utc_now
from .budget import BudgetPreview
from .case_numbers import CaseNumberAllocator
from .models import PlanSnapshot, PromptBundle
from .recipes import RecipeResolver, recipe_binding_hash
from .strategies import StrategyRegistry

PROMPT_TEXT_KIND = "prompt_text"
PROMPT_REF_KINDS = {"prompt_image", "prompt_video", "mask_video"}


def generation_signature(case: dict[str, Any]) -> str:
    """Stable identity for one billable generation input.

    Plan lineage, Case numbering and unrelated assets are intentionally
    excluded so a resume cannot become a new paid submission by accident.
    """

    return content_hash(
        {
            "feed_asset_id": case.get("feed_asset_id"),
            "prompt_text": case.get("prompt_text", ""),
            "prompt_asset_ids": sorted(case.get("prompt_asset_ids", [])),
            "recipe_id": case.get("operation_recipe_id"),
            "recipe_version": case.get("operation_recipe_version"),
            "mode": case.get("generation_mode"),
            "repeat_index": case.get("repeat_index"),
            "model_id": case.get("model_id"),
            "generation_config": case.get("generation_config", {}),
            "api_asset_bindings": case.get("api_asset_bindings", {}),
            "edited_video_asset_id": case.get("edited_video_asset_id"),
            "expected_audio_source_asset_id": case.get("expected_audio_source_asset_id"),
        }
    )


class TestPlanBuilder:
    # The public domain class name is intentional; keep pytest from treating
    # it as a test container during collection.
    __test__ = False

    def __init__(
        self,
        repository: Any,
        recipes: RecipeResolver,
        scenario_pack: dict[str, Any],
        benchmark: dict[str, Any],
        allocator: CaseNumberAllocator | None = None,
        strategy_registry: StrategyRegistry | None = None,
        clock: Any = None,
    ) -> None:
        self._repository = repository
        self._recipes = recipes
        self._scenario_pack = scenario_pack
        self._benchmark = benchmark
        self._allocator = allocator or CaseNumberAllocator(repository)
        self._strategies = strategy_registry or StrategyRegistry.defaults()
        self._clock = clock

    # ------------------------------------------------------------------
    def preview(self, request: dict[str, Any]) -> dict[str, Any]:
        assets = self._load_assets(request)
        feeds, prompt_bundles, skipped = self._prepare(assets, request)
        cases, skipped_cases = self._expand(feeds, prompt_bundles, request)
        selection = self._selection(request)
        budget = BudgetPreview(request.get("cost_config")).preview(
            cases=[case for case, _ in cases],
            skipped=skipped + skipped_cases,
            repeat_count=(
                1 if selection["strategy"] == "random_runs" else request.get("repeat_count", 5)
            ),
        )
        budget["feed_count"] = len(feeds)
        budget["prompt_bundle_count"] = len(prompt_bundles)
        budget["allocation"] = self._allocation_summary(request, cases)
        return budget

    def build(self, request: dict[str, Any]) -> dict[str, Any]:
        assets = self._load_assets(request)
        feeds, prompt_bundles, skipped = self._prepare(assets, request)
        cases, skipped_cases = self._expand(feeds, prompt_bundles, request)
        all_skipped = skipped + skipped_cases

        model_id = request.get("model_id", "x2.0")
        repeat_count = int(request.get("repeat_count", 5))
        seed = self._derive_seed(request)
        selection = self._selection(request)
        plan_hash_payload = {
            # Batch IDs are provenance only. The selected generation inputs
            # define identity; later result assets must not invalidate resume.
            "generation_signatures": sorted(generation_signature(case) for case, _ in cases),
            "benchmark_version": self._benchmark.get("benchmark_version"),
            "scenario_pack_version": self._scenario_pack.get("version"),
            "recipe_pack_version": self._recipes.pack_version,
            "model_id": model_id,
            "repeat_count": repeat_count,
            "generation_modes": sorted(request.get("generation_modes", ["offline"])),
            "generation_config": request.get("generation_config", {}),
            "filters": request.get("filters", {}),
            "combination_selection": selection,
            "seed": seed,
        }
        plan_hash = content_hash(plan_hash_payload)
        plan_id = request.get("plan_id") or f"plan-{plan_hash[:12]}"

        # A TestPlan is a frozen immutable snapshot: the same selected inputs
        # must always yield the same plan.  Rebuilding it after a crash would
        # re-read the current case-number suffix and produce shifted
        # ``case_number`` values while keeping stable task IDs, which breaks
        # idempotent task persistence.  Reuse the already-frozen plan instead.
        existing_plan = self._repository.find_plan_by_hash(plan_hash)
        if existing_plan is not None:
            return existing_plan

        snapshot = PlanSnapshot(
            plan_id=plan_id,
            plan_version="1",
            plan_hash=plan_hash,
            input_asset_batch_ids=tuple(sorted(request.get("asset_batch_ids", []))),
            benchmark_version=self._benchmark.get("benchmark_version", ""),
            scenario_pack_version=self._scenario_pack.get("version", ""),
            seed=seed,
            cases=tuple(case for case, _ in cases),
            created_at=self._clock.now() if self._clock else utc_now(),
            metadata={
                "model_id": model_id,
                "repeat_count": repeat_count,
                "effective_repeat_count": (
                    1 if selection["strategy"] == "random_runs" else repeat_count
                ),
                "allocation": self._allocation_summary(request, cases),
                "recipe_pack_version": self._recipes.pack_version,
                "skipped": all_skipped,
                "skipped_count": len(all_skipped),
            },
        )
        plan = snapshot.to_dict()
        plan["frozen"] = True
        task_batch = TaskAllocator(self._repository, self._clock).create(plan)
        plan["task_batch_id"] = task_batch["task_batch_id"]
        plan["task_ids"] = task_batch["task_ids"]
        self._repository.save_test_plan(plan)
        return plan

    # ------------------------------------------------------------------
    def _load_assets(self, request: dict[str, Any]) -> list[dict[str, Any]]:
        return _expand_asset_bindings(self._repository.list_assets())

    def _prepare(
        self, assets: list[dict[str, Any]], request: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], list[PromptBundle], list[dict[str, Any]]]:
        filters = request.get("filters", {})
        ready = [asset for asset in assets if asset.get("status") == "ready"]
        feeds = [asset for asset in ready if asset.get("kind") in {"feed_video", "feed_image"}]
        if filters.get("feed_asset_ids"):
            feeds = [asset for asset in feeds if asset["asset_id"] in filters["feed_asset_ids"]]
        feeds.sort(
            key=lambda item: (
                str(item.get("metadata", {}).get("record_number") or ""),
                item["asset_id"],
            )
        )
        feed_limit = filters.get("feed_limit")
        if feed_limit is not None:
            feeds = feeds[: int(feed_limit)]

        text_assets = [asset for asset in ready if asset.get("kind") == PROMPT_TEXT_KIND]
        ref_assets = [asset for asset in ready if asset.get("kind") in PROMPT_REF_KINDS]

        skipped: list[dict[str, Any]] = []
        for asset in assets:
            if asset.get("status") != "ready":
                skipped.append(
                    {
                        "asset_id": asset.get("asset_id"),
                        "reason": f"asset status {asset.get('status')!r} is not ready",
                    }
                )

        bundles = self._group_prompt_bundles(text_assets, ref_assets, skipped)
        bundles.sort(
            key=lambda item: (
                str(item.metadata.get("record_number") or ""),
                item.prompt_text,
                item.prompt_asset_ids,
            )
        )
        if filters.get("prompt_record_numbers"):
            allowed_prompt_numbers = {str(value) for value in filters["prompt_record_numbers"]}
            bundles = [
                bundle
                for bundle in bundles
                if str(bundle.metadata.get("record_number")) in allowed_prompt_numbers
            ]
        prompt_limit = filters.get("prompt_limit")
        if prompt_limit is not None:
            bundles = bundles[: int(prompt_limit)]
        numbered: list[PromptBundle] = []
        for index, bundle in enumerate(bundles, start=1):
            numbered.append(
                PromptBundle(
                    prompt_number=(
                        f"prompt{str(bundle.metadata.get('record_number')).zfill(3)}"
                        if bundle.metadata.get("record_number")
                        else self._allocator.prompt_number(index)
                    ),
                    prompt_text=bundle.prompt_text,
                    prompt_asset_ids=bundle.prompt_asset_ids,
                    play_name=bundle.play_name,
                    scenario_id=bundle.scenario_id,
                    metadata={**bundle.metadata, "prompt_index": index},
                )
            )
        return feeds, numbered, skipped

    def _group_prompt_bundles(
        self,
        text_assets: list[dict[str, Any]],
        ref_assets: list[dict[str, Any]],
        skipped: list[dict[str, Any]],
    ) -> list[PromptBundle]:
        groups: dict[str, list[dict[str, Any]]] = {}
        for asset in text_assets + ref_assets:
            key = _group_key(asset)
            groups.setdefault(key, []).append(asset)

        bundles: list[PromptBundle] = []
        for key, members in sorted(groups.items()):
            texts = [item for item in members if item.get("kind") == PROMPT_TEXT_KIND]
            refs = [item for item in members if item.get("kind") in PROMPT_REF_KINDS]
            if not texts:
                for member in members:
                    skipped.append(
                        {
                            "asset_id": member.get("asset_id"),
                            "reason": "prompt reference has no prompt_text in its group",
                        }
                    )
                continue
            refs.sort(key=lambda item: item["asset_id"])
            for text in texts:
                metadata = text.get("metadata", {})
                bundles.append(
                    PromptBundle(
                        prompt_number="prompt000",  # assigned by caller
                        prompt_text=str(metadata.get("text", "") or "").strip(),
                        prompt_asset_ids=tuple(item["asset_id"] for item in refs),
                        play_name=metadata.get("play_name"),
                        scenario_id=metadata.get("scenario_id"),
                        metadata={
                            **metadata,
                            "reference_kinds": sorted(
                                {item.get("kind") for item in refs if item.get("kind")}
                            ),
                        },
                    )
                )
        return bundles

    def _expand(
        self,
        feeds: list[dict[str, Any]],
        bundles: list[PromptBundle],
        request: dict[str, Any],
    ) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], list[dict[str, Any]]]:
        """Resolve eligible pairs, allocate them, then freeze exact Cases."""

        overrides = {
            item.get("play_name"): item.get("mode")
            for item in request.get("generation_mode_overrides", [])
        }
        model_id = request.get("model_id", "x2.0")
        repeat_count = int(request.get("repeat_count", 5))
        seed = self._derive_seed(request)
        enabled_modes = set(request.get("generation_modes", ["offline"]))

        candidates: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for feed_index, feed in enumerate(feeds, start=1):
            feed_record_number = feed.get("metadata", {}).get("record_number")
            feed_number = (
                f"feed{str(feed_record_number).zfill(3)}"
                if feed_record_number
                else self._allocator.feed_number(feed_index)
            )
            for bundle in bundles:
                recipe, reason = self._recipes.resolve_for_prompt(bundle, overrides=overrides)
                if reason:
                    skipped.append(
                        {
                            "feed_asset_id": feed["asset_id"],
                            "prompt_text": bundle.prompt_text,
                            "reason": reason,
                        }
                    )
                    continue
                try:
                    mode = self._recipes.resolve_mode(recipe, request_overrides=overrides)
                except Exception as exc:
                    skipped.append(
                        {
                            "feed_asset_id": feed["asset_id"],
                            "prompt_text": bundle.prompt_text,
                            "reason": str(exc),
                        }
                    )
                    continue
                if mode not in enabled_modes:
                    skipped.append(
                        {
                            "feed_asset_id": feed["asset_id"],
                            "prompt_text": bundle.prompt_text,
                            "reason": f"generation mode {mode} is disabled by the run request",
                        }
                    )
                    continue
                binding = self._recipes.bindings(recipe, mode)
                combo = self._resolve_combo(feed, bundle, recipe, mode, binding, request)
                if combo.get("skip_reason"):
                    skipped.append(
                        {
                            "feed_asset_id": feed["asset_id"],
                            "prompt_text": bundle.prompt_text,
                            "reason": combo["skip_reason"],
                        }
                    )
                    continue
                candidates.append(
                    {
                        "pair_key": content_hash(
                            {
                                "feed_asset_id": feed["asset_id"],
                                "prompt_text": bundle.prompt_text,
                                "prompt_asset_ids": sorted(bundle.prompt_asset_ids),
                                "recipe_id": recipe["recipe_id"],
                                "mode": mode,
                            }
                        ),
                        "feed": feed,
                        "feed_number": feed_number,
                        "bundle": bundle,
                        "prompt_record_number": str(bundle.metadata.get("record_number") or ""),
                        "recipe": recipe,
                        "mode": mode,
                        "binding": binding,
                        "combo": combo,
                    }
                )

        selection = self._selection(request)
        strategy = self._strategies.get(selection["strategy"])
        selected = strategy.select(
            candidates,
            selection,
            repeat_count=repeat_count,
            seed=seed,
        )
        expanded: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for item in selected:
            candidate = item.candidate
            prefix = self._allocator.prefix_for(
                candidate["feed_number"], candidate["bundle"].prompt_number
            )
            existing_max = self._allocator.existing_max_suffix(prefix)
            case = self._freeze_case(
                candidate["feed"],
                candidate["bundle"],
                candidate["recipe"],
                candidate["mode"],
                candidate["binding"],
                candidate["combo"],
                item.repeat_index,
                item.repeat_count,
                existing_max,
                candidate["feed_number"],
                model_id,
                seed,
                request,
            )
            case["allocation_index"] = item.allocation_index
            case["allocation_strategy"] = strategy.name
            expanded.append((case, candidate["combo"]))
        return expanded, skipped

    def _selection(self, request: dict[str, Any]) -> dict[str, Any]:
        selection = dict(request.get("combination_selection") or {})
        selection.setdefault("strategy", "cartesian")
        return selection

    def _allocation_summary(
        self,
        request: dict[str, Any],
        cases: list[tuple[dict[str, Any], dict[str, Any]]],
    ) -> dict[str, Any]:
        selection = self._selection(request)
        strategy = self._strategies.get(selection["strategy"])
        pair_keys = {
            (
                case.get("feed_asset_id"),
                case.get("prompt_number"),
                case.get("operation_recipe_id"),
                case.get("generation_mode"),
            )
            for case, _ in cases
        }
        return {
            "strategy": strategy.name,
            "strategy_version": strategy.version,
            "selection": selection,
            "selected_pair_count": len(pair_keys),
            "task_count": len(cases),
        }

    def _resolve_combo(
        self,
        feed: dict[str, Any],
        bundle: PromptBundle,
        recipe: dict[str, Any],
        mode: str,
        binding: dict[str, str],
        request: dict[str, Any],
    ) -> dict[str, Any]:
        ref_video_path = binding.get("refVideoPath")
        ref_image_path = binding.get("refImagePath")
        edited_role = recipe.get("edited_video_role")
        audio_role = recipe.get("expected_audio_source_role")

        prompt_video_ids = [
            aid for aid in bundle.prompt_asset_ids if aid in self._prompt_video_ids()
        ]
        prompt_image_ids = [
            aid for aid in bundle.prompt_asset_ids if aid in self._prompt_image_ids()
        ]

        if ref_video_path == "prompt_video" and not prompt_video_ids:
            return {"skip_reason": f"recipe {recipe['recipe_id']} requires a prompt video"}
        if ref_image_path == "prompt_image" and not prompt_image_ids:
            return {"skip_reason": f"recipe {recipe['recipe_id']} requires a prompt image"}
        if (
            mode == "offline"
            and ref_video_path == "feed_video"
            and feed.get("kind") != "feed_video"
        ):
            return {
                "skip_reason": (
                    f"recipe {recipe['recipe_id']} requires a video Feed; got {feed.get('kind')}"
                )
            }

        edited_video_asset_id = (
            feed["asset_id"]
            if edited_role == "feed_video" or edited_role == "realtime_input_stream"
            else prompt_video_ids[0]
            if prompt_video_ids
            else None
        )
        expected_audio_source_asset_id = (
            feed["asset_id"]
            if audio_role == "feed_video" or audio_role == "realtime_input_stream"
            else prompt_video_ids[0]
            if prompt_video_ids
            else None
        )
        if not edited_video_asset_id or not expected_audio_source_asset_id:
            return {"skip_reason": "cannot freeze edited video / audio source"}
        scenario_id = self._resolve_scenario(bundle, recipe, mode)
        cost = self._estimate_generation_cost(
            feed=feed,
            prompt_video_ids=prompt_video_ids,
            mode=mode,
            ref_video_path=ref_video_path,
            request=request,
        )
        return {
            "edited_video_asset_id": edited_video_asset_id,
            "expected_audio_source_asset_id": expected_audio_source_asset_id,
            "scenario_id": scenario_id,
            "scene_tags": self._scene_tags(scenario_id),
            "api_bindings": binding,
            "prompt_video_ids": prompt_video_ids,
            "prompt_image_ids": prompt_image_ids,
            **cost,
        }

    def _estimate_generation_cost(
        self,
        *,
        feed: dict[str, Any],
        prompt_video_ids: list[str],
        mode: str,
        ref_video_path: str | None,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        """Estimate credits with the documented XMAX billing formula."""

        generation = request.get("generation_config", {})
        if mode == "realtime":
            seconds = float(generation.get("duration_s", 3.0))
            return {
                "estimated_billable_duration_s": seconds,
                "estimated_credits": int(math.ceil(seconds)),
                "credit_formula": "realtime_seconds_x_1",
            }
        source = feed
        if ref_video_path == "prompt_video" and prompt_video_ids:
            source = self._repository.get_asset(prompt_video_ids[0])
        seconds = min(float(source.get("media", {}).get("duration_s") or 0.0), 600.0)
        quality = str(generation.get("quality", "hd"))
        quality_factor = 1.5 if quality == "hd" else 1.0
        fps = int(generation.get("fps", 24))
        credits = max(math.floor(quality_factor * fps / 24 * seconds), 5)
        return {
            "estimated_billable_duration_s": seconds,
            "estimated_credits": credits,
            "credit_formula": f"offline_{quality}_{fps}fps",
        }

    def _freeze_case(
        self,
        feed: dict[str, Any],
        bundle: PromptBundle,
        recipe: dict[str, Any],
        mode: str,
        binding: dict[str, str],
        combo: dict[str, Any],
        repeat_index: int,
        repeat_count: int,
        existing_max: int,
        feed_number: str,
        model_id: str,
        seed: int,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        prompt_number = bundle.prompt_number
        prefix = self._allocator.prefix_for(feed_number, prompt_number)
        case_number = self._allocator.allocate(
            prefix,
            repeat_index=repeat_index,
            repeat_count=repeat_count,
            existing_max_suffix=existing_max,
        )
        case_key = {
            "feed_asset_id": feed["asset_id"],
            "prompt_text": bundle.prompt_text,
            "prompt_asset_ids": sorted(bundle.prompt_asset_ids),
            "recipe_id": recipe["recipe_id"],
            "recipe_version": recipe.get("version"),
            "mode": mode,
            "repeat_index": repeat_index,
            "model_id": model_id,
            "generation_config": {
                **request.get("generation_config", {}),
                "estimated_billable_duration_s": combo.get("estimated_billable_duration_s"),
                "estimated_credits": combo.get("estimated_credits"),
                "credit_formula": combo.get("credit_formula"),
            },
            "binding_hash": recipe_binding_hash(recipe, mode, binding),
            "seed": seed,
        }
        case_id = f"case_{content_hash(case_key)[:16]}"
        frozen = {
            "case_id": case_id,
            "case_number": case_number,
            "feed_number": feed_number,
            "prompt_number": prompt_number,
            "feed_asset_id": feed["asset_id"],
            "prompt_asset_ids": list(bundle.prompt_asset_ids),
            "prompt_text": bundle.prompt_text,
            "generation_mode": mode,
            "repeat_index": repeat_index,
            "model_id": model_id,
            "operation_recipe_id": recipe["recipe_id"],
            "operation_recipe_version": recipe.get("version", ""),
            "evaluation_operation_contract": recipe.get("evaluation_contract", {}),
            "edited_video_asset_id": combo["edited_video_asset_id"],
            "expected_audio_source_asset_id": combo["expected_audio_source_asset_id"],
            "api_asset_bindings": combo["api_bindings"],
            "scenario_id": combo["scenario_id"],
            "scenario_pack_version": self._scenario_pack.get("version", ""),
            "scene_tags": combo["scene_tags"],
            "generation_config": {
                **request.get("generation_config", {}),
                "estimated_billable_duration_s": combo.get("estimated_billable_duration_s"),
                "estimated_credits": combo.get("estimated_credits"),
                "credit_formula": combo.get("credit_formula"),
            },
        }
        frozen["generation_signature"] = generation_signature(frozen)
        return frozen

    def _resolve_scenario(
        self, bundle: PromptBundle, recipe: dict[str, Any], mode: str
    ) -> str | None:
        if bundle.scenario_id:
            return bundle.scenario_id
        play_name = bundle.play_name
        for scenario in self._scenario_pack.get("scenarios", []):
            if play_name and play_name in scenario.get("name", ""):
                if mode in scenario.get("supported_modes", []):
                    return scenario["scenario_id"]
        # Deterministic fallback: first scenario supporting the mode whose
        # description mentions the recipe's edited role.
        for scenario in self._scenario_pack.get("scenarios", []):
            if mode in scenario.get("supported_modes", []) and ("换" in scenario.get("name", "")):
                return scenario["scenario_id"]
        for scenario in self._scenario_pack.get("scenarios", []):
            if mode in scenario.get("supported_modes", []):
                return scenario["scenario_id"]
        return None

    def _scene_tags(self, scenario_id: str | None) -> dict[str, str]:
        if scenario_id is None:
            return {}
        for scenario in self._scenario_pack.get("scenarios", []):
            if scenario.get("scenario_id") == scenario_id:
                return dict(scenario.get("tags", {}))
        return {}

    def _prompt_video_ids(self) -> set[str]:
        return {
            asset["asset_id"]
            for asset in _expand_asset_bindings(self._repository.list_assets())
            if asset.get("kind") == "prompt_video"
        }

    def _prompt_image_ids(self) -> set[str]:
        return {
            asset["asset_id"]
            for asset in _expand_asset_bindings(self._repository.list_assets())
            if asset.get("kind") == "prompt_image"
        }

    def _derive_seed(self, request: dict[str, Any]) -> int:
        if request.get("seed") is not None:
            return int(request["seed"])
        payload = {
            "recipe_pack_version": self._recipes.pack_version,
            "scenario_pack_version": self._scenario_pack.get("version"),
            "filters": request.get("filters", {}),
            "generation_modes": sorted(request.get("generation_modes", ["offline"])),
            "generation_mode_overrides": request.get("generation_mode_overrides", []),
            "generation_config": request.get("generation_config", {}),
            "combination_selection": self._selection(request),
        }
        digest = hashlib.sha256(content_hash(payload).encode("utf-8")).hexdigest()
        return int(digest[:8], 16)


def _group_key(asset: dict[str, Any]) -> str:
    metadata = asset.get("metadata", {})
    for key in ("group_id", "record_id", "source_record_id"):
        if metadata.get(key):
            return f"{key}:{metadata[key]}"
    return f"own:{asset['asset_id']}"


def _expand_asset_bindings(assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Expand one immutable file into its Feishu business-role bindings.

    A byte-identical file may legitimately be both a Feed and a Prompt
    reference, or be referenced by multiple Prompt records. The artifact is
    stored once by SHA-256 while planning sees every record-scoped role.
    """

    expanded: list[dict[str, Any]] = []
    for asset in assets:
        metadata = asset.get("metadata", {})
        bindings = metadata.get("bindings") or []
        if not bindings:
            expanded.append(asset)
            continue
        for binding in bindings:
            expanded.append(
                {
                    **asset,
                    "kind": binding.get("kind", asset.get("kind")),
                    "metadata": {
                        **metadata,
                        **binding,
                        "physical_asset_kind": asset.get("kind"),
                    },
                }
            )
    return expanded
