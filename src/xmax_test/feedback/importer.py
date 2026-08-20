"""Human signal import.

Raw text is immutable; normalized labels are derived and versioned. The
``review_context`` must distinguish blind vs ai_assisted; missing values are
recorded as ``unknown`` and never guessed.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from ..errors import ContractError
from ..time import utc_now

VALID_SOURCES = {
    "human_dataset",
    "independent_human_eval",
    "evaluation_feedback",
    "human_confirmation",
}
VALID_CONTEXTS = {"blind", "ai_assisted", "not_applicable", "unknown"}


class HumanSignalImporter:
    def __init__(self, repository: Any, clock: Any = None) -> None:
        self._repository = repository
        self._clock = clock

    def import_file(self, path: str | Path) -> dict[str, Any]:
        """Import human signals from a JSON file (list of signal objects)."""

        path = Path(path)
        if not path.is_file():
            raise ContractError(f"human signal file missing: {path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        items = data if isinstance(data, list) else data.get("signals", [])
        return self.import_records(items, source_file=str(path))

    def import_records(
        self, records: list[dict[str, Any]], source_file: str = "inline"
    ) -> dict[str, Any]:
        imported: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for index, record in enumerate(records):
            try:
                signal = self._normalize_record(record, source_file)
                self._repository.append_human_signal(signal)
                imported.append(signal)
            except Exception as exc:
                errors.append(
                    {
                        "code": "xmax.contract_error",
                        "message": f"record {index}: {exc}",
                        "stage": "feedback",
                        "retryable": False,
                    }
                )
        return {"imported": len(imported), "errors": errors, "signals": imported}

    def _normalize_record(self, record: dict[str, Any], source_file: str) -> dict[str, Any]:
        raw_text = record.get("raw_text")
        if not isinstance(raw_text, str) or not raw_text.strip():
            raise ContractError("human signal requires non-empty raw_text")
        source_type = record.get("source_type", "evaluation_feedback")
        if source_type not in VALID_SOURCES:
            raise ContractError(f"invalid source_type: {source_type!r}")
        review_context = record.get("review_context", "unknown")
        if review_context not in VALID_CONTEXTS:
            raise ContractError(f"invalid review_context: {review_context!r}")
        # Preserve caller-supplied supervision such as time ranges, ROI,
        # keypoints and Feed/Prompt/Result asset references. These fields are
        # essential to downstream CV/MLLM trainers and remain immutable source
        # evidence; controlled identity fields below always win.
        return {
            **record,
            "signal_id": record.get("signal_id") or f"signal-{uuid.uuid4().hex[:16]}",
            "sample_id": record.get("sample_id", ""),
            "evaluation_id": record.get("evaluation_id"),
            "source_type": source_type,
            "raw_text": raw_text,
            "review_context": review_context,
            "mapping_status": record.get("mapping_status", "needs_clarification"),
            "normalized_labels": record.get("normalized_labels", []),
            "dimension_proposal": record.get("dimension_proposal"),
            "learning_permission": bool(record.get("learning_permission", False)),
            "data_partition": record.get("data_partition"),
            "normalizer_id": record.get("normalizer_id"),
            "normalizer_version": record.get("normalizer_version"),
            "source_file": source_file,
            "imported_at": utc_now(),
        }
