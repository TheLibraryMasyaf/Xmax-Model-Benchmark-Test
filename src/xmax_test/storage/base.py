"""Append-only metadata and artifact repository boundaries."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol


class MetadataRepository(Protocol):
    def append(self, collection: str, record: dict[str, Any]) -> None:
        """Append a versioned domain record or event."""

    def get(self, collection: str, record_id: str) -> dict[str, Any]:
        """Read one record by stable business ID."""


class ArtifactStore(Protocol):
    def put(self, namespace: str, source: Path, metadata: dict[str, Any]) -> str:
        """Store an artifact and return its durable URI."""

    def resolve(self, uri: str) -> Path:
        """Resolve a local artifact URI for a worker."""

