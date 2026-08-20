"""Asset source and registry adapter boundaries."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol


class AssetSource(Protocol):
    def list_assets(self, query: dict[str, Any]) -> list[dict[str, Any]]:
        """Discover remote or local source records without downloading them."""

    def download(self, remote: dict[str, Any], destination: Path) -> dict[str, Any]:
        """Download one asset and return bytes/hash/source metadata."""


class AssetRegistry(Protocol):
    def upsert(self, asset: dict[str, Any]) -> dict[str, Any]:
        """Register a validated content-addressed asset."""

    def get(self, asset_id: str) -> dict[str, Any]:
        """Return one immutable asset version."""

