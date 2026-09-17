"""Materialize the XLive pairwise Feishu dataset into the generic pack contract.

The table relationship is intentionally isolated here.  The downstream pack
importer only sees frozen field snapshots and stable sample references, so a
future source may resolve two, three or more Feishu tables without changing the
HumanSignal contract.
"""

from __future__ import annotations

from typing import Any

from ...errors import ContractError
from ...hashing import content_hash
from ...time import utc_now


def build_pack(client: Any, config: dict[str, Any]) -> dict[str, Any]:
    options = config.get("options") or {}
    app_token = config["app_token"]
    review_table = _required(options, "review_table_id")
    sample_table = _required(options, "sample_table_id")
    review_fields = options.get("review_fields") or {}
    sample_fields = options.get("sample_fields") or {}
    sides = options.get("sides") or {}

    required_review = {
        "test_result_id",
        "purpose",
        "reviewer",
        "created_at",
        "evaluation_number",
        "sample_link",
        "preference",
    }
    required_sample = {"test_result_id", "scenario", "prompt_text", "feed_video", "prompt_asset"}
    if missing := sorted(required_review - set(review_fields)):
        raise ContractError(f"XLive review field mapping missing: {missing}")
    if missing := sorted(required_sample - set(sample_fields)):
        raise ContractError(f"XLive sample field mapping missing: {missing}")
    if set(sides) != {"xmax", "decart"}:
        raise ContractError("XLive sides must define exactly xmax and decart")
    for side, mapping in sides.items():
        if not mapping.get("video_field") or not mapping.get("comment_field"):
            raise ContractError(f"XLive {side} side requires video_field and comment_field")
        if not mapping.get("model_version"):
            raise ContractError(f"XLive {side} side requires model_version")

    review_meta = _field_metadata(client, app_token, review_table)
    sample_meta = _field_metadata(client, app_token, sample_table)
    reviews = _all_records(client, app_token, review_table)
    samples = _all_records(client, app_token, sample_table)
    samples_by_id = {str(item.get("record_id")): item for item in samples}

    reviewer_user_id = _required(options, "reviewer_user_id")
    purpose_value = str(options.get("purpose_value") or "人工评价")
    eligible: list[dict[str, Any]] = []
    for record in reviews:
        fields = record.get("fields") or {}
        if _scalar(fields.get(review_fields["purpose"])) != purpose_value:
            continue
        if reviewer_user_id not in _user_ids(fields.get(review_fields["reviewer"])):
            continue
        eligible.append(record)

    latest: dict[str, dict[str, Any]] = {}
    for record in eligible:
        fields = record.get("fields") or {}
        test_result_id = str(_scalar(fields.get(review_fields["test_result_id"])) or "").strip()
        if not test_result_id:
            continue
        order = (
            _order_value(_scalar(fields.get(review_fields["created_at"]))),
            _order_value(_scalar(fields.get(review_fields["evaluation_number"]))),
            str(record.get("record_id") or ""),
        )
        current = latest.get(test_result_id)
        if current is None or order > current["_order"]:
            latest[test_result_id] = {**record, "_order": order}

    result_rows: dict[str, dict[str, Any]] = {}
    evaluations: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    complete_comments = 0
    for test_result_id in sorted(latest):
        review = latest[test_result_id]
        review_record_id = str(review.get("record_id") or "")
        fields = review.get("fields") or {}
        linked_ids = _link_ids(fields.get(review_fields["sample_link"]))
        if len(linked_ids) != 1:
            skipped.append(
                {
                    "test_result_id": test_result_id,
                    "record_id": review_record_id,
                    "reason": "sample_link_must_resolve_exactly_one_record",
                    "linked_record_ids": linked_ids,
                }
            )
            continue
        sample = samples_by_id.get(linked_ids[0])
        if sample is None:
            skipped.append(
                {
                    "test_result_id": test_result_id,
                    "record_id": review_record_id,
                    "reason": "linked_sample_not_found",
                }
            )
            continue
        sample_record_id = str(sample.get("record_id") or "")
        sample_values = sample.get("fields") or {}
        linked_test_result_id = str(
            _scalar(sample_values.get(sample_fields["test_result_id"])) or ""
        ).strip()
        if linked_test_result_id != test_result_id:
            skipped.append(
                {
                    "test_result_id": test_result_id,
                    "record_id": review_record_id,
                    "reason": "linked_sample_business_key_mismatch",
                    "linked_test_result_id": linked_test_result_id,
                }
            )
            continue

        participants = []
        sample_ids: dict[str, str] = {}
        missing_video = False
        for side in ("xmax", "decart"):
            side_config = sides[side]
            video_field = side_config["video_field"]
            attachment = _first_attachment(sample_values.get(video_field))
            if attachment is None:
                missing_video = True
                skipped.append(
                    {
                        "test_result_id": test_result_id,
                        "record_id": review_record_id,
                        "side": side,
                        "reason": "missing_result_video",
                    }
                )
                break
            token = str(attachment.get("file_token") or attachment.get("token") or "")
            sample_id = "result-" + content_hash(
                {
                    "base_token": app_token,
                    "table_id": sample_table,
                    "record_id": sample_record_id,
                    "field_id": sample_meta[video_field]["id"],
                    "attachment_token": token,
                }
            )[:20]
            sample_ids[side] = sample_id
            if sample_id not in result_rows:
                result_rows[sample_id] = {
                    "sample_id": sample_id,
                    "test_result_id": test_result_id,
                    "side": side,
                    "model_version": side_config["model_version"],
                    "fields": {
                        "video": _snapshot(
                            sample_table,
                            sample_record_id,
                            video_field,
                            [attachment],
                            sample_meta,
                        ),
                        "scenario": _snapshot(
                            sample_table,
                            sample_record_id,
                            sample_fields["scenario"],
                            sample_values.get(sample_fields["scenario"]),
                            sample_meta,
                        ),
                        "prompt_text": _snapshot(
                            sample_table,
                            sample_record_id,
                            sample_fields["prompt_text"],
                            sample_values.get(sample_fields["prompt_text"]),
                            sample_meta,
                        ),
                        "feed_video": _snapshot(
                            sample_table,
                            sample_record_id,
                            sample_fields["feed_video"],
                            sample_values.get(sample_fields["feed_video"]),
                            sample_meta,
                        ),
                        "prompt_asset": _snapshot(
                            sample_table,
                            sample_record_id,
                            sample_fields["prompt_asset"],
                            sample_values.get(sample_fields["prompt_asset"]),
                            sample_meta,
                        ),
                    },
                }
            comment_field = side_config["comment_field"]
            participants.append(
                {
                    "sample_id": sample_id,
                    "side": side,
                    "comment": _snapshot(
                        review_table,
                        review_record_id,
                        comment_field,
                        fields.get(comment_field),
                        review_meta,
                    ),
                }
            )
        if missing_video:
            continue

        comments = [str(item["comment"].get("value") or "").strip() for item in participants]
        if all(comments):
            complete_comments += 1
        preference_value = str(_scalar(fields.get(review_fields["preference"])) or "").strip()
        if preference_value == sides["xmax"].get("preference_value", "Xmax"):
            relation = "preference"
            better, worse = sample_ids["xmax"], sample_ids["decart"]
        elif preference_value == sides["decart"].get("preference_value", "Decart"):
            relation = "preference"
            better, worse = sample_ids["decart"], sample_ids["xmax"]
        elif preference_value == str(options.get("equivalent_value") or "差不多"):
            relation = "equivalent"
            better = worse = None
        else:
            skipped.append(
                {
                    "test_result_id": test_result_id,
                    "record_id": review_record_id,
                    "reason": "invalid_preference",
                    "value": preference_value,
                }
            )
            continue
        comparison_id = "comparison-" + content_hash(
            {
                "source_id": config["source_id"],
                "record_id": review_record_id,
                "test_result_id": test_result_id,
                "reviewed_at": fields.get(review_fields["created_at"]),
            }
        )[:20]
        evaluations.append(
            {
                "comparison_id": comparison_id,
                "test_result_id": test_result_id,
                "source_record_id": review_record_id,
                "review_context": config.get("review_context", "unknown"),
                "reviewer": _snapshot(
                    review_table,
                    review_record_id,
                    review_fields["reviewer"],
                    fields.get(review_fields["reviewer"]),
                    review_meta,
                ),
                "reviewed_at": _snapshot(
                    review_table,
                    review_record_id,
                    review_fields["created_at"],
                    fields.get(review_fields["created_at"]),
                    review_meta,
                ),
                "preference": {
                    "relation": relation,
                    "better_sample_id": better,
                    "worse_sample_id": worse,
                    "source": _snapshot(
                        review_table,
                        review_record_id,
                        review_fields["preference"],
                        fields.get(review_fields["preference"]),
                        review_meta,
                    ),
                },
                "participants": participants,
            }
        )

    return {
        "$schema": "../../schemas/human-evaluation-import-pack.schema.json",
        "pack_version": "1.0",
        "source_id": config["source_id"],
        "base_token": app_token,
        "created_at": utc_now(),
        "results": sorted(result_rows.values(), key=lambda item: item["sample_id"]),
        "evaluations": sorted(evaluations, key=lambda item: item["test_result_id"]),
        "audit": {
            "review_records_read": len(reviews),
            "sample_records_read": len(samples),
            "eligible_review_records": len(eligible),
            "latest_unique_test_results": len(latest),
            "materialized_comparisons": len(evaluations),
            "materialized_results": len(result_rows),
            "comparisons_with_complete_comments": complete_comments,
            "comparisons_without_complete_comments": len(evaluations) - complete_comments,
            "skipped": skipped,
        },
    }


def _required(mapping: dict[str, Any], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"XLive preprocessor requires options.{key}")
    return value


def _field_metadata(client: Any, app_token: str, table_id: str) -> dict[str, dict[str, Any]]:
    fields = client.get_fields(app_token, table_id)
    return {str(item.get("name")): item for item in fields if item.get("name")}


def _all_records(client: Any, app_token: str, table_id: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    page_token: str | None = None
    while True:
        page = client.get_bitable_records(app_token, table_id, page_token=page_token)
        records.extend(page.get("records", []))
        if not page.get("has_more"):
            return records
        page_token = page.get("page_token")
        if page_token is None:
            raise ContractError(f"Feishu table {table_id} has_more without a page token")


def _snapshot(
    table_id: str,
    record_id: str,
    field_name: str,
    value: Any,
    metadata: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    field = metadata.get(field_name)
    if field is None or not field.get("id"):
        raise ContractError(f"Feishu field not found in {table_id}: {field_name}")
    frozen = _freeze_value(value)
    return {
        "source": {
            "table_id": table_id,
            "record_id": record_id,
            "field_id": str(field["id"]),
            "field_name": field_name,
        },
        "value": frozen,
        "value_hash": content_hash(frozen),
    }


def _freeze_value(value: Any) -> Any:
    if isinstance(value, dict):
        allowed = {
            key: _freeze_value(item)
            for key, item in value.items()
            if key not in {"tmp_url", "temporary_url", "url"}
        }
        return allowed
    if isinstance(value, list):
        return [_freeze_value(item) for item in value]
    return value


def _scalar(value: Any) -> Any:
    if isinstance(value, list):
        if not value:
            return None
        first = value[0]
        if isinstance(first, dict):
            return first.get("text") or first.get("name") or first.get("id")
        return first
    if isinstance(value, dict):
        return value.get("text") or value.get("name") or value.get("id")
    return value


def _user_ids(value: Any) -> set[str]:
    if isinstance(value, list):
        return {
            str(item.get("id") or item.get("user_id") or item.get("open_id"))
            for item in value
            if isinstance(item, dict)
            and (item.get("id") or item.get("user_id") or item.get("open_id"))
        }
    if isinstance(value, dict) and (
        value.get("id") or value.get("user_id") or value.get("open_id")
    ):
        return {str(value.get("id") or value.get("user_id") or value.get("open_id"))}
    return set()


def _link_ids(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item.get("id")) for item in value if isinstance(item, dict) and item.get("id")]
    if isinstance(value, dict) and value.get("id"):
        return [str(value["id"])]
    return []


def _first_attachment(value: Any) -> dict[str, Any] | None:
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict) and (item.get("file_token") or item.get("token")):
                return item
    if isinstance(value, dict) and (value.get("file_token") or value.get("token")):
        return value
    return None


def _order_value(value: Any) -> tuple[int, float | str]:
    try:
        return (1, float(value))
    except (TypeError, ValueError):
        return (0, str(value or ""))
