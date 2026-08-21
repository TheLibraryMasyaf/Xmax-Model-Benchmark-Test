"""Feishu adapter that expands attachment fields into video + comment samples."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..errors import ContractError
from ..hashing import content_hash


class FeishuHumanDatasetImporter:
    def __init__(
        self,
        client: Any,
        media_importer: Any,
        *,
        download_root: str | Path,
    ) -> None:
        self._client = client
        self._media_importer = media_importer
        self._download_root = Path(download_root)

    def import_source(self, config: dict[str, Any]) -> dict[str, Any]:
        app_token = config["app_token"]
        table_id = config["table_id"]
        comment_field = config["comment_field"]
        video_fields = list(config["video_fields"])
        records = self._all_records(app_token, table_id)
        samples = []
        skipped = []
        download_errors = []
        for record in records:
            record_id = record.get("record_id", "")
            fields = record.get("fields", {})
            comment = fields.get(comment_field)
            if not isinstance(comment, str) or not comment.strip():
                skipped.append({"record_id": record_id, "reason": "empty_comment"})
                continue
            for field in video_fields:
                attachments = fields.get(field)
                if not isinstance(attachments, list) or not attachments:
                    skipped.append(
                        {
                            "record_id": record_id,
                            "field": field,
                            "reason": "missing_video",
                        }
                    )
                    continue
                first = attachments[0]
                if not isinstance(first, dict):
                    skipped.append(
                        {
                            "record_id": record_id,
                            "field": field,
                            "reason": "invalid_attachment",
                        }
                    )
                    continue
                token = first.get("file_token") or first.get("token")
                if not token:
                    skipped.append(
                        {
                            "record_id": record_id,
                            "field": field,
                            "reason": "missing_token",
                        }
                    )
                    continue
                suffix = Path(first.get("name") or "video.mp4").suffix or ".mp4"
                signal_id = (
                    "signal-"
                    + content_hash(
                        {
                            "app_token": app_token,
                            "table_id": table_id,
                            "record_id": record_id,
                            "field": field,
                            "token": token,
                        }
                    )[:16]
                )
                destination = self._download_root / record_id / f"{signal_id}{suffix}"
                destination.parent.mkdir(parents=True, exist_ok=True)
                try:
                    if destination.is_file() and destination.stat().st_size > 0:
                        downloaded = {"path": str(destination), "cached": True}
                    else:
                        downloaded = self._client.download_attachment(
                            token,
                            destination,
                            app_token=app_token,
                            table_id=table_id,
                            record_id=record_id,
                        )
                except Exception as exc:
                    download_errors.append(
                        {"record_id": record_id, "field": field, "error": str(exc)}
                    )
                    continue
                samples.append(
                    {
                        "signal_id": signal_id,
                        "sample_id": signal_id,
                        "video_path": downloaded["path"],
                        "raw_text": comment.strip(),
                        "source_type": "independent_human_eval",
                        "review_context": config.get("review_context", "unknown"),
                        "source_id": config.get("source_id", "feishu-human-feedback"),
                        "source_record_id": record_id,
                        "source_group_id": record_id,
                        "source_field": field,
                        "attachment_token": token,
                    }
                )
        outcome = self._media_importer.import_records(
            samples, source_file=f"feishu://{app_token}/{table_id}"
        )
        return {
            **outcome,
            "remote_records": len(records),
            "candidate_samples": len(samples),
            "skipped": skipped,
            "download_errors": download_errors,
        }

    def _all_records(self, app_token: str, table_id: str) -> list[dict[str, Any]]:
        records = []
        page_token = None
        while True:
            page = self._client.list_records(app_token, table_id, page_token)
            records.extend(page.get("records", []))
            if not page.get("has_more"):
                return records
            page_token = page.get("page_token")
            if page_token is None:
                raise ContractError("Feishu human table has_more=true without page_token")
