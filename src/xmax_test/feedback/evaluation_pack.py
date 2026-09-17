"""Import a frozen human-evaluation pack into immutable HumanSignals.

The source-specific preprocessor resolves tables and joins.  This module only
understands the generic pack contract: a canonical pairwise evaluation and
stable result references.  Each usable side comment becomes an absolute
single-video signal while retaining the pairwise relation as provenance.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..errors import ContractError
from ..hashing import content_hash


class HumanEvaluationPackImporter:
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

    def import_pack(
        self,
        pack: dict[str, Any],
        *,
        source_file: str = "human-evaluation-pack",
        download_workers: int = 1,
    ) -> dict[str, Any]:
        _verify_pack_integrity(pack)
        results = {item["sample_id"]: item for item in pack.get("results", [])}
        records: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        required_sample_ids = {
            str(participant["sample_id"])
            for evaluation in pack.get("evaluations", [])
            for participant in evaluation.get("participants") or []
            if str((participant.get("comment") or {}).get("value") or "").strip()
        }
        downloads, download_errors = self._download_required(
            pack, results, required_sample_ids, workers=download_workers
        )
        complete = 0
        pairwise_only = 0

        for evaluation in pack.get("evaluations", []):
            participants = evaluation.get("participants") or []
            nonempty = [
                item
                for item in participants
                if str((item.get("comment") or {}).get("value") or "").strip()
            ]
            if len(nonempty) == len(participants):
                complete += 1
            if not nonempty:
                pairwise_only += 1
                continue
            participant_ids = [str(item.get("sample_id") or "") for item in participants]
            if len(set(participant_ids)) != len(participant_ids):
                raise ContractError(
                    f"comparison {evaluation.get('comparison_id')} has duplicate participants"
                )
            for participant in nonempty:
                sample_id = str(participant["sample_id"])
                result = results.get(sample_id)
                if result is None:
                    raise ContractError(
                        f"comparison {evaluation.get('comparison_id')} references unknown "
                        f"sample {sample_id}"
                    )
                counterpart_ids = [item for item in participant_ids if item != sample_id]
                if len(counterpart_ids) != 1:
                    raise ContractError(
                        f"comparison {evaluation.get('comparison_id')} must have two sides"
                    )
                attachment = _first_attachment(result["fields"]["video"].get("value"))
                video_path = downloads.get(sample_id)
                if attachment is None or video_path is None:
                    skipped.append(
                        {
                            "comparison_id": evaluation.get("comparison_id"),
                            "sample_id": sample_id,
                            "reason": "missing_or_failed_result_attachment",
                        }
                    )
                    continue
                comment = participant["comment"]
                signal_id = "signal-" + content_hash(
                    {
                        "comparison_id": evaluation["comparison_id"],
                        "sample_id": sample_id,
                        "comment_hash": comment["value_hash"],
                    }
                )[:20]
                relation = _project_relation(evaluation["preference"], sample_id)
                scenario = _scalar(result["fields"]["scenario"].get("value"))
                records.append(
                    {
                        "signal_id": signal_id,
                        "sample_id": sample_id,
                        "signal_kind": "pairwise_projection",
                        "video_path": str(video_path),
                        "raw_text": str(comment.get("value") or "").strip(),
                        "source_type": "evaluation_feedback",
                        "review_context": evaluation.get("review_context", "unknown"),
                        "source_id": pack["source_id"],
                        "source_record_id": evaluation["source_record_id"],
                        "source_group_id": _source_group(evaluation["test_result_id"]),
                        "source_field": comment["source"]["field_name"],
                        "attachment_token": _attachment_token(attachment),
                        "model_version": result.get("model_version"),
                        "scenario": scenario,
                        "comparison": {
                            "comparison_id": evaluation["comparison_id"],
                            "counterpart_sample_id": counterpart_ids[0],
                            "relation": relation,
                        },
                        "source_provenance": {
                            "pack_source_id": pack["source_id"],
                            "base_token": pack["base_token"],
                            "result_video": result["fields"]["video"]["source"],
                            "comment": comment["source"],
                            "preference": evaluation["preference"]["source"]["source"],
                            "reviewer": evaluation["reviewer"]["source"],
                            "reviewed_at": evaluation["reviewed_at"]["source"],
                        },
                        "annotations": {
                            "test_result_id": evaluation["test_result_id"],
                            "side": participant.get("side"),
                            "comparison_relation": relation,
                            "prompt_text": result["fields"]["prompt_text"],
                            "feed_video": result["fields"]["feed_video"],
                            "prompt_asset": result["fields"]["prompt_asset"],
                        },
                    }
                )

        outcome = self._media_importer.import_records(records, source_file=source_file)
        return {
            **outcome,
            "comparisons": len(pack.get("evaluations", [])),
            "comparisons_with_complete_comments": complete,
            "pairwise_only_comparisons": pairwise_only,
            "candidate_projections": len(records),
            "skipped": skipped,
            "download_errors": download_errors,
        }

    def _download_required(
        self,
        pack: dict[str, Any],
        results: dict[str, dict[str, Any]],
        sample_ids: set[str],
        *,
        workers: int,
    ) -> tuple[dict[str, Path], list[dict[str, Any]]]:
        if workers < 1 or workers > 8:
            raise ContractError("download_workers must be between 1 and 8")

        def fetch(sample_id: str) -> tuple[str, Path]:
            result = results.get(sample_id)
            if result is None:
                raise ContractError(f"unknown result sample {sample_id}")
            attachment = _first_attachment(result["fields"]["video"].get("value"))
            if attachment is None:
                raise ContractError(f"sample {sample_id} has no result attachment")
            return sample_id, self._download(pack, result, attachment)

        downloaded: dict[str, Path] = {}
        errors: list[dict[str, Any]] = []
        if workers == 1:
            for sample_id in sorted(sample_ids):
                try:
                    key, path = fetch(sample_id)
                    downloaded[key] = path
                except Exception as exc:
                    errors.append({"sample_id": sample_id, "error": str(exc)})
            return downloaded, errors

        from concurrent.futures import ThreadPoolExecutor, as_completed

        with ThreadPoolExecutor(max_workers=workers) as executor:
            pending = {executor.submit(fetch, sample_id): sample_id for sample_id in sample_ids}
            for future in as_completed(pending):
                sample_id = pending[future]
                try:
                    key, path = future.result()
                    downloaded[key] = path
                except Exception as exc:
                    errors.append({"sample_id": sample_id, "error": str(exc)})
        return downloaded, sorted(errors, key=lambda item: item["sample_id"])

    def _download(
        self, pack: dict[str, Any], result: dict[str, Any], attachment: dict[str, Any]
    ) -> Path:
        token = _attachment_token(attachment)
        if not token:
            raise ContractError(f"sample {result['sample_id']} attachment has no token")
        name = str(attachment.get("name") or "result.mp4")
        suffix = Path(name).suffix or ".mp4"
        destination = self._download_root / result["sample_id"] / f"result{suffix}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        expected_size = int(attachment.get("size") or 0)
        if (
            destination.is_file()
            and destination.stat().st_size > 0
            and (expected_size <= 0 or destination.stat().st_size == expected_size)
        ):
            return destination
        source = result["fields"]["video"]["source"]
        downloaded = self._client.download_attachment(
            token,
            destination,
            app_token=source.get("base_token") or pack["base_token"],
            table_id=source["table_id"],
            record_id=source["record_id"],
        )
        path = Path(downloaded.get("path") or destination)
        if not path.is_file() or path.stat().st_size <= 0:
            raise ContractError(f"downloaded attachment is empty: {path}")
        if expected_size > 0 and path.stat().st_size != expected_size:
            raise ContractError(
                f"downloaded attachment size mismatch: {path} "
                f"expected={expected_size} actual={path.stat().st_size}"
            )
        return path


def _project_relation(preference: dict[str, Any], sample_id: str) -> str:
    if preference.get("relation") == "equivalent":
        return "equivalent_to"
    if preference.get("better_sample_id") == sample_id:
        return "better_than"
    if preference.get("worse_sample_id") == sample_id:
        return "worse_than"
    raise ContractError(f"preference does not reference participant {sample_id}")


def _source_group(test_result_id: str) -> str:
    match = re.search(r"feed[-_ ]?\d+", str(test_result_id), flags=re.IGNORECASE)
    if match:
        return match.group(0).lower().replace("_", "-").replace(" ", "-")
    return str(test_result_id)


def _attachment_token(attachment: dict[str, Any]) -> str:
    return str(attachment.get("file_token") or attachment.get("token") or "")


def _first_attachment(value: Any) -> dict[str, Any] | None:
    if isinstance(value, list):
        return next((item for item in value if isinstance(item, dict)), None)
    return value if isinstance(value, dict) else None


def _scalar(value: Any) -> Any:
    if isinstance(value, list):
        if not value:
            return None
        return _scalar(value[0])
    if isinstance(value, dict):
        return value.get("text") or value.get("name") or value.get("id")
    return value


def _verify_pack_integrity(pack: dict[str, Any]) -> None:
    result_ids = [str(item.get("sample_id") or "") for item in pack.get("results", [])]
    comparison_ids = [
        str(item.get("comparison_id") or "") for item in pack.get("evaluations", [])
    ]
    if len(result_ids) != len(set(result_ids)):
        raise ContractError("human evaluation pack has duplicate sample_id values")
    if len(comparison_ids) != len(set(comparison_ids)):
        raise ContractError("human evaluation pack has duplicate comparison_id values")

    snapshots: list[dict[str, Any]] = []
    for result in pack.get("results", []):
        snapshots.extend((result.get("fields") or {}).values())
    for evaluation in pack.get("evaluations", []):
        snapshots.extend([evaluation["reviewer"], evaluation["reviewed_at"]])
        snapshots.append(evaluation["preference"]["source"])
        snapshots.extend(item["comment"] for item in evaluation.get("participants", []))
    for snapshot in snapshots:
        actual = content_hash(snapshot.get("value"))
        if actual != snapshot.get("value_hash"):
            source = snapshot.get("source") or {}
            raise ContractError(
                "human evaluation pack field hash mismatch: "
                f"{source.get('table_id')}/{source.get('record_id')}/"
                f"{source.get('field_name')}"
            )
