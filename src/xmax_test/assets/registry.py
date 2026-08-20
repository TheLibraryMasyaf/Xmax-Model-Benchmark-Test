"""Content-addressed asset registry.

Assets are keyed by content SHA-256; identical content is idempotent and
changed content always produces a new asset_id. Media validation decides
``ready`` vs ``invalid`` vs ``quarantined``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..errors import ContractError, ValidationError
from ..hashing import sha256_text
from ..time import utc_now
from .models import DownloadResult, RemoteAsset, remote_asset_dict
from .validator import MediaValidator, sniff_mime


def asset_id_from_hash(sha256: str) -> str:
    return f"asset_{sha256[:16]}"


class AssetRegistry:
    def __init__(
        self,
        repository: Any,
        artifact_store: Any,
        validator: MediaValidator | None = None,
        clock: Any = None,
    ) -> None:
        self._repository = repository
        self._artifacts = artifact_store
        self._validator = validator or MediaValidator()
        self._clock = clock

    def register_download(
        self, source: dict[str, Any], download: DownloadResult
    ) -> dict[str, Any]:
        """Register one downloaded file as a content-addressed asset.

        Returns the asset record; ``None``-free idempotent path when the same
        sha256 already exists.
        """

        kind = download.metadata.get("kind", source.get("asset_kind", "feed_video"))
        binding = {
            "kind": kind,
            "source_id": source.get("source_id"),
            "source_kind": source.get("kind"),
            "remote_key": download.remote_key,
            "revision": download.revision,
            "attachment_token": download.attachment_token,
            **download.metadata,
        }
        existing = self._repository.find_asset_by_sha256(download.sha256)
        if existing is not None:
            metadata = dict(existing.get("metadata", {}))
            bindings = list(metadata.get("bindings", []))
            binding_key = _binding_key(binding)
            if not any(_binding_key(item) == binding_key for item in bindings):
                bindings.append(binding)
                metadata["bindings"] = bindings
                existing = {**existing, "metadata": metadata}
                self._repository.upsert_asset(existing)
            return existing

        asset_id = asset_id_from_hash(download.sha256)
        mime = sniff_mime(download.path)
        source_meta = {
            "source_id": source.get("source_id"),
            "source_kind": source.get("kind"),
            "remote_key": download.remote_key,
            "revision": download.revision,
            "attachment_token": download.attachment_token,
            **download.metadata,
        }

        status = "ready"
        media: dict[str, Any] = {}
        if kind != "prompt_text":
            try:
                media = self._validator.validate(download.path, kind)
            except ValidationError:
                status = "invalid"
            except Exception:  # probe infrastructure unavailable -> cannot verify
                status = "quarantined"

        stored = self._artifacts.put_file(
            "assets",
            download.path,
            f"{asset_id}/source.bin",
            expected_sha256=download.sha256,
        )
        asset = {
            "asset_id": asset_id,
            "kind": kind,
            "uri": stored["uri"],
            "sha256": download.sha256,
            "bytes": stored["bytes"],
            "mime_type": mime,
            "source": source_meta,
            "status": status,
            "media": media,
            "metadata": {
                **download.metadata,
                "filename": download.path.name,
                "source": source_meta,
                "bindings": [binding],
            },
            "created_at": utc_now(),
        }
        self._repository.upsert_asset(asset)
        return asset


def _binding_key(binding: dict[str, Any]) -> tuple[str, str, str, str]:
    """Stable business identity for one use of a content-addressed file."""

    return (
        str(binding.get("source_id") or ""),
        str(binding.get("remote_key") or ""),
        str(binding.get("record_id") or binding.get("group_id") or ""),
        str(binding.get("kind") or ""),
    )

    def verify(self, asset: dict[str, Any]) -> dict[str, Any]:
        """Re-verify hash and media of a stored asset."""

        verification = self._artifacts.verify(asset["uri"], expected_sha256=asset["sha256"])
        try:
            media = self._validator.validate(
                self._artifacts.resolve(asset["uri"]), asset["kind"]
            )
            status = "ready"
        except ValidationError:
            status = "invalid"
            media = {}
        self._repository.update_asset_status(asset["asset_id"], status)
        return {**verification, "status": status, "media": media}

    def register_remote(
        self, source: dict[str, Any], remote: RemoteAsset, download_dir: Path
    ) -> dict[str, Any]:
        """High-level helper: download + register a remote asset."""

        target = download_dir / f"{remote.remote_key}"
        downloaded = _download_via_source(source, remote, target)
        return self.register_download(source, downloaded)


def _download_via_source(
    source: dict[str, Any], remote: RemoteAsset, target: Path
) -> DownloadResult:
    downloader = source.get("_downloader")
    if downloader is None:
        raise ContractError(f"source {source.get('source_id')} has no downloader")
    raw = downloader.download(remote, target)
    return DownloadResult(
        remote_key=raw.get("remote_key", remote.remote_key),
        path=Path(raw["path"]),
        sha256=raw["sha256"],
        bytes=int(raw.get("bytes", 0)),
        revision=raw.get("revision", remote.revision),
        attachment_token=raw.get("attachment_token", remote.attachment_token),
        metadata=raw.get("metadata", remote.metadata),
    )


class SourceRunner:
    """Runs discover / sync / verify over configured asset sources."""

    def __init__(
        self,
        registry: AssetRegistry,
        source_factory: Any,
        download_dir: Path,
        clock: Any = None,
    ) -> None:
        self._registry = registry
        self._source_factory = source_factory
        self._download_dir = Path(download_dir)
        self._download_dir.mkdir(parents=True, exist_ok=True)
        self._clock = clock

    def discover(
        self, sources_config: dict[str, Any], query: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """List remote records without downloading anything."""

        discovered: list[dict[str, Any]] = []
        for item in sources_config.get("sources", []):
            if not item.get("enabled", False):
                continue
            source = self._source_factory(item)
            for remote in source.list_assets(query or {}):
                discovered.append(remote_asset_dict(remote))
        return discovered

    def sync(
        self, sources_config: dict[str, Any], query: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Download, validate and register all enabled sources' assets.

        Idempotent: identical content hashes are skipped; changed content
        registers a new asset_id.
        """

        summary: dict[str, Any] = {
            "sources": [],
            "assets_registered": 0,
            "assets_skipped": 0,
            "errors": [],
        }
        for item in sources_config.get("sources", []):
            if not item.get("enabled", False):
                continue
            source = self._source_factory(item)
            source_entry: dict[str, Any] = {
                "source_id": item.get("source_id"),
                "kind": item.get("kind"),
                "discovered": 0,
                "registered": 0,
                "skipped": 0,
                "failed": 0,
            }
            try:
                remotes = source.list_assets(query or {})
            except Exception as exc:
                source_entry["failed"] = 1
                summary["errors"].append(
                    {"code": "xmax.external_failure", "message": str(exc), "stage": "ingest", "retryable": True}
                )
                summary["sources"].append(source_entry)
                continue
            source_entry["discovered"] = len(remotes)
            for remote in remotes:
                target = self._download_dir / item["source_id"] / remote.remote_key
                try:
                    downloaded = source.download(remote, target)
                    pre_existing = self._registry._repository.find_asset_by_sha256(
                        downloaded.sha256
                    )
                    asset = self._registry.register_download(
                        {
                            "source_id": item.get("source_id"),
                            "kind": item.get("kind"),
                            "asset_kind": item.get("asset_kind"),
                        },
                        downloaded,
                    )
                    if pre_existing is not None:
                        source_entry["skipped"] += 1
                        summary["assets_skipped"] += 1
                    else:
                        source_entry["registered"] += 1
                        summary["assets_registered"] += 1
                    source_entry.setdefault("status_by_id", {})[
                        remote.remote_key
                    ] = asset["status"]
                except Exception as exc:
                    source_entry["failed"] += 1
                    summary["errors"].append(
                        {
                            "code": "xmax.external_failure",
                            "message": f"{remote.remote_key}: {exc}",
                            "stage": "ingest",
                            "retryable": True,
                            "entity_id": remote.remote_key,
                        }
                    )
            summary["sources"].append(source_entry)
        return summary

    def verify_batch(self, asset_ids: list[str]) -> dict[str, Any]:
        """Verify the listed assets: hash plus media re-validation."""

        results = []
        for asset_id in asset_ids:
            try:
                asset = self._registry._repository.get_asset(asset_id)
                results.append({"asset_id": asset_id, **self._registry.verify(asset)})
            except Exception as exc:
                results.append({"asset_id": asset_id, "error": str(exc)})
        return {"verified": len(results), "results": results}
