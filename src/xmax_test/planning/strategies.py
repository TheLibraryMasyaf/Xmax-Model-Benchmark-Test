"""Pluggable, deterministic Feed x Prompt allocation strategies.

Planning is the only place where combinations are chosen.  Workers consume
the frozen result and never sample again, which keeps paid runs reproducible.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any, Protocol

from ..errors import ContractError


@dataclass(frozen=True)
class SelectedCombination:
    candidate: dict[str, Any]
    repeat_index: int
    repeat_count: int
    allocation_index: int


class AllocationStrategy(Protocol):
    name: str
    version: str

    def select(
        self,
        candidates: list[dict[str, Any]],
        selection: dict[str, Any],
        *,
        repeat_count: int,
        seed: int,
    ) -> list[SelectedCombination]: ...


class StrategyRegistry:
    """Registry boundary for adding allocation policies without editing the builder."""

    def __init__(self) -> None:
        self._strategies: dict[str, AllocationStrategy] = {}

    def register(self, strategy: AllocationStrategy) -> None:
        if not strategy.name:
            raise ContractError("allocation strategy requires a name")
        self._strategies[strategy.name] = strategy

    def get(self, name: str) -> AllocationStrategy:
        try:
            return self._strategies[name]
        except KeyError as exc:
            raise ContractError(f"unknown combination allocation strategy: {name}") from exc

    @classmethod
    def defaults(cls) -> StrategyRegistry:
        registry = cls()
        for strategy in (
            CartesianStrategy(),
            RandomPairsStrategy(),
            RandomRunsStrategy(),
            ExplicitPairsStrategy(),
        ):
            registry.register(strategy)
        return registry


def _expand_repeats(
    candidates: list[dict[str, Any]], repeat_count: int
) -> list[SelectedCombination]:
    if repeat_count < 1:
        raise ContractError("repeat_count must be at least 1")
    totals: dict[str, int] = {}
    for candidate in candidates:
        key = candidate["pair_key"]
        totals[key] = totals.get(key, 0) + repeat_count
    seen: dict[str, int] = {}
    result: list[SelectedCombination] = []
    allocation_index = 0
    for candidate in candidates:
        key = candidate["pair_key"]
        for _ in range(repeat_count):
            allocation_index += 1
            seen[key] = seen.get(key, 0) + 1
            result.append(
                SelectedCombination(
                    candidate=candidate,
                    repeat_index=seen[key],
                    repeat_count=totals[key],
                    allocation_index=allocation_index,
                )
            )
    return result


class CartesianStrategy:
    name = "cartesian"
    version = "1"

    def select(self, candidates, selection, *, repeat_count, seed):
        return _expand_repeats(candidates, repeat_count)


class RandomPairsStrategy:
    name = "random_pairs"
    version = "1"

    def select(self, candidates, selection, *, repeat_count, seed):
        target = int(selection.get("target_pair_count", 0))
        if target < 1:
            raise ContractError("random_pairs requires target_pair_count >= 1")
        with_replacement = bool(selection.get("with_replacement", False))
        if not candidates:
            raise ContractError("no eligible Feed x Prompt combinations")
        if not with_replacement and target > len(candidates):
            raise ContractError(
                f"target_pair_count {target} exceeds {len(candidates)} eligible pairs"
            )
        rng = random.Random(seed)
        chosen = (
            [rng.choice(candidates) for _ in range(target)]
            if with_replacement
            else rng.sample(candidates, target)
        )
        return _expand_repeats(chosen, repeat_count)


class RandomRunsStrategy:
    name = "random_runs"
    version = "1"

    def select(self, candidates, selection, *, repeat_count, seed):
        """Choose exactly target_run_count attempts.

        ``repeat_count`` is deliberately not multiplied here.  Repeated draws
        of the same pair receive deterministic repeat indices and Case suffixes.
        """

        target = int(selection.get("target_run_count", 0))
        if target < 1:
            raise ContractError("random_runs requires target_run_count >= 1")
        if not candidates:
            raise ContractError("no eligible Feed x Prompt combinations")
        with_replacement = bool(selection.get("with_replacement", True))
        if not with_replacement and target > len(candidates):
            raise ContractError(
                f"target_run_count {target} exceeds {len(candidates)} eligible pairs"
            )
        rng = random.Random(seed)
        chosen = (
            [rng.choice(candidates) for _ in range(target)]
            if with_replacement
            else rng.sample(candidates, target)
        )
        totals: dict[str, int] = {}
        for candidate in chosen:
            totals[candidate["pair_key"]] = totals.get(candidate["pair_key"], 0) + 1
        seen: dict[str, int] = {}
        result: list[SelectedCombination] = []
        for allocation_index, candidate in enumerate(chosen, start=1):
            key = candidate["pair_key"]
            seen[key] = seen.get(key, 0) + 1
            result.append(
                SelectedCombination(
                    candidate=candidate,
                    repeat_index=seen[key],
                    repeat_count=totals[key],
                    allocation_index=allocation_index,
                )
            )
        return result


class ExplicitPairsStrategy:
    name = "explicit_pairs"
    version = "1"

    def select(self, candidates, selection, *, repeat_count, seed):
        requested = selection.get("pairs") or []
        if not requested:
            raise ContractError("explicit_pairs requires a non-empty pairs list")
        by_key = {
            (item["feed"]["asset_id"], str(item["prompt_record_number"])): item
            for item in candidates
        }
        chosen: list[dict[str, Any]] = []
        missing: list[str] = []
        seen_requested: set[tuple[str, str]] = set()
        for pair in requested:
            key = (
                str(pair.get("feed_asset_id", "")),
                str(pair.get("prompt_record_number", "")),
            )
            if key in seen_requested:
                raise ContractError(
                    f"explicit_pairs contains duplicate pair: {key[0]} + prompt record {key[1]}"
                )
            seen_requested.add(key)
            candidate = by_key.get(key)
            if candidate is None:
                missing.append(f"{key[0]} + prompt record {key[1]}")
            else:
                chosen.append(candidate)
        if missing:
            raise ContractError(
                "explicit pair is not eligible after filters/recipe/mode checks: "
                + "; ".join(missing)
            )
        return _expand_repeats(chosen, repeat_count)
