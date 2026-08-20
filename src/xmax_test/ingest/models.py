"""Existing-result import domain models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ImportedCase:
    """One frozen source record mapped to import inputs."""

    source_key: str
    case_number: str
    model_version: str
    prompt_text: str | None = None
    generation_mode: str | None = None
    operation_recipe_id: str | None = None
    result_download: dict[str, Any] | None = None
    feed_download: dict[str, Any] | None = None
    prompt_attachment_download: dict[str, Any] | None = None
    source_record_id: str | None = None
    source_attachment_tokens: tuple[str, ...] = ()
    provenance: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


@dataclass
class ImportOutcome:
    import_request_id: str
    source_hash: str
    status: str  # completed | skipped | partial | error
    imported: int = 0
    skipped: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)
    run_batch_id: str | None = None
    asset_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "import_request_id": self.import_request_id,
            "source_hash": self.source_hash,
            "status": self.status,
            "imported": self.imported,
            "skipped": self.skipped,
            "errors": self.errors,
            "run_batch_id": self.run_batch_id,
            "asset_ids": self.asset_ids,
        }
