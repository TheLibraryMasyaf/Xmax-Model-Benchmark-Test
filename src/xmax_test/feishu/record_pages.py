"""Normalize lark-cli Bitable record-list envelopes.

Current lark-cli returns projected rows as a matrix plus a field-name list and
record-id list.  Older fakes and adapters use ``records=[{fields: ...}]``.
Keep both shapes accepted at the boundary so the rest of the pipeline always
receives record dictionaries.
"""

from __future__ import annotations

from typing import Any


def normalize_record_page(data: Any, *, offset: int = 0) -> dict[str, Any]:
    if isinstance(data, list):
        records = data
        has_more = len(records) == 200
    elif isinstance(data, dict) and ("records" in data or "items" in data):
        records = data.get("records") or data.get("items") or []
        has_more = bool(data.get("has_more", len(records) == 200))
    elif isinstance(data, dict) and isinstance(data.get("data"), list):
        rows = data.get("data") or []
        field_names = data.get("fields") or data.get("field_name_list") or []
        record_ids = data.get("record_id_list") or []
        records = []
        for index, row in enumerate(rows):
            values = row if isinstance(row, list) else []
            fields = {
                str(name): values[column] if column < len(values) else None
                for column, name in enumerate(field_names)
            }
            record_id = record_ids[index] if index < len(record_ids) else f"offset-{offset + index}"
            records.append({"record_id": record_id, "fields": fields})
        has_more = bool(data.get("has_more", False))
    else:
        records = []
        has_more = False
    return {
        "records": records,
        "has_more": has_more,
        "page_token": str(offset + len(records)) if has_more else None,
    }
