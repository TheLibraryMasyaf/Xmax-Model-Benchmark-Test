"""Operation recipe resolution.

The runner must never guess which video is edited or which API field binds the
prompt video. Every case freezes recipe ID/version, edited video role, expected
audio source role and API bindings.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..errors import ConfigError, ContractError
from ..hashing import content_hash
from .models import PromptBundle


class RecipeResolver:
    def __init__(self, pack_path: str | object) -> None:
        data = json.loads(Path(pack_path).read_text(encoding="utf-8"))
        if data.get("recipe_pack_id") != "xmax-default-operation-recipes":
            # Any valid pack is acceptable; the ID is just provenance.
            pass
        self._pack = data
        self._recipes = {item["recipe_id"]: item for item in data.get("recipes", [])}
        self._by_play: dict[str, str] = {}
        for recipe in data.get("recipes", []):
            for play_name in recipe.get("play_names", []):
                if play_name in self._by_play:
                    raise ConfigError(f"duplicate play_name binding: {play_name}")
                self._by_play[play_name] = recipe["recipe_id"]

    @property
    def pack_version(self) -> str:
        return self._pack.get("version", "")

    def recipe(self, recipe_id: str) -> dict[str, Any]:
        try:
            return self._recipes[recipe_id]
        except KeyError as exc:
            raise ContractError(f"unknown operation recipe: {recipe_id}") from exc

    def resolve_for_prompt(
        self,
        prompt: PromptBundle,
        *,
        overrides: dict[str, str] | None = None,
    ) -> tuple[dict[str, Any], str | None]:
        """Resolve the recipe for a prompt.

        Play name is looked up in the prompt's metadata, then in the prompt
        text. Returns ``(recipe, skip_reason)``; an unmatched prompt yields a
        skip reason instead of a guess.
        """

        overrides = overrides or {}
        play_name = None
        if prompt.play_name:
            play_name = (
                prompt.play_name
                if prompt.play_name in self._by_play
                else _match_play_name(prompt.play_name, self._by_play)
            )
        play_name = play_name or _match_play_name(prompt.prompt_text, self._by_play)
        reference_kinds = set(prompt.metadata.get("reference_kinds", []))
        if "prompt_video" in reference_kinds:
            video_recipe = next(
                (
                    recipe
                    for recipe in self._recipes.values()
                    if recipe.get("bindings", {}).get("offline", {}).get("refVideoPath")
                    == "prompt_video"
                ),
                None,
            )
            if video_recipe is not None:
                inferred_play = play_name or next(iter(video_recipe.get("play_names", [])), None)
                override = overrides.get(video_recipe["recipe_id"]) or (
                    overrides.get(inferred_play) if inferred_play else None
                )
                if override:
                    if override not in video_recipe.get("allowed_generation_modes", []):
                        return {}, (
                            f"recipe {video_recipe['recipe_id']} does not allow "
                            f"explicit mode {override}"
                        )
                    video_recipe = {
                        **video_recipe,
                        "default_generation_mode": override,
                    }
                return video_recipe, None
        if "prompt_image" in reference_kinds and play_name is None:
            image_recipe = self._recipes.get("offline-image-reference")
            if image_recipe is not None:
                return image_recipe, None
        if not reference_kinds and play_name is None:
            text_recipe = self._recipes.get("offline-text-edit")
            if text_recipe is not None:
                return text_recipe, None
        if play_name is None:
            return {}, f"prompt {prompt.prompt_number} matches no play name"
        recipe_id = self._by_play[play_name]
        recipe = self.recipe(recipe_id)
        override = overrides.get(recipe_id) or overrides.get(play_name)
        if override:
            if override not in recipe.get("allowed_generation_modes", []):
                return {}, (f"recipe {recipe_id} does not allow explicit mode {override}")
            recipe = {**recipe, "default_generation_mode": override}
        return recipe, None

    def resolve_mode(
        self,
        recipe: dict[str, Any],
        *,
        explicit_mode: str | None = None,
        request_overrides: dict[str, str] | None = None,
    ) -> str:
        """Mode priority: explicit case mode -> request override -> recipe default."""

        recipe_id = recipe["recipe_id"]
        request_overrides = request_overrides or {}
        if explicit_mode is not None:
            if explicit_mode not in recipe.get("allowed_generation_modes", []):
                raise ContractError(
                    f"explicit mode {explicit_mode} not allowed by recipe {recipe_id}"
                )
            return explicit_mode
        requested = request_overrides.get(recipe_id) or request_overrides.get(
            recipe.get("play_names", [""])[0]
        )
        if requested is not None:
            if requested not in recipe.get("allowed_generation_modes", []):
                raise ContractError(f"request mode {requested} not allowed by recipe {recipe_id}")
            return requested
        default = recipe.get("default_generation_mode")
        if default not in recipe.get("allowed_generation_modes", []):
            raise ContractError(f"recipe {recipe_id} default mode {default} not in allowed modes")
        return default

    def bindings(self, recipe: dict[str, Any], mode: str) -> dict[str, str]:
        bindings = recipe.get("bindings", {}).get(mode)
        if not isinstance(bindings, dict):
            raise ContractError(f"recipe {recipe['recipe_id']} has no bindings for mode {mode}")
        return {key: str(value) for key, value in bindings.items()}


def _match_play_name(prompt_text: str, by_play: dict[str, str]) -> str | None:
    for play_name in by_play:
        if play_name and play_name in prompt_text:
            return play_name
    return None


def recipe_binding_hash(recipe: dict[str, Any], mode: str, bindings: dict[str, str]) -> str:
    return content_hash(
        {
            "recipe_id": recipe["recipe_id"],
            "recipe_version": recipe.get("version"),
            "mode": mode,
            "edited_video_role": recipe.get("edited_video_role"),
            "expected_audio_source_role": recipe.get("expected_audio_source_role"),
            "bindings": bindings,
        }
    )
