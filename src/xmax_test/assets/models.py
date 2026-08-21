"""Asset domain models and source descriptors."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ASSET_KINDS = (
    "feed_video",
    "feed_image",
    "prompt_text",
    "prompt_image",
    "prompt_video",
    "mask_video",
    "result_video",
    "event_clip",
)

ASSET_STATUSES = (
    "discovered",
    "downloading",
    "downloaded",
    "validating",
    "ready",
    "invalid",
    "quarantined",
)


@dataclass(frozen=True)
class RemoteAsset:
    """A record discovered from a source, not yet downloaded."""

    source_id: str
    remote_key: str
    kind: str
    asset_kind: str
    revision: str | None = None
    attachment_token: str | None = None
    row_index: int | None = None
    record_id: str | None = None
    url: str | None = None
    filename: str | None = None
    bytes: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DownloadResult:
    remote_key: str
    path: Path
    sha256: str
    bytes: int
    revision: str | None = None
    attachment_token: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SourceDescriptor:
    """Config descriptor for one source entry in asset-sources.json."""

    source_id: str
    kind: str
    enabled: bool
    asset_kind: str
    config: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_config(cls, item: dict[str, Any]) -> SourceDescriptor:
        return cls(
            source_id=item["source_id"],
            kind=item["kind"],
            enabled=bool(item.get("enabled", False)),
            asset_kind=item["asset_kind"],
            config={
                key: value
                for key, value in item.items()
                if key not in {"source_id", "kind", "enabled", "asset_kind"}
            },
        )


def remote_asset_dict(remote: RemoteAsset) -> dict[str, Any]:
    return {
        "source_id": remote.source_id,
        "remote_key": remote.remote_key,
        "kind": remote.kind,
        "asset_kind": remote.asset_kind,
        "revision": remote.revision,
        "attachment_token": remote.attachment_token,
        "row_index": remote.row_index,
        "record_id": remote.record_id,
        "url": remote.url,
        "filename": remote.filename,
        "bytes": remote.bytes,
        "metadata": remote.metadata,
    }
