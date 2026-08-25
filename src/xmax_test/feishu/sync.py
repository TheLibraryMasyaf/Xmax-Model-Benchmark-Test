"""Feishu projection sync.

Default projection covers Feed data, Prompt data and Case data. Each
GenerationRun maps to exactly one Case row keyed by ``case编号 + Xmax模型版本``.
Internal scores are 0-100; Feishu stores 0-1 (divide by 100 on write,
multiply by 100 on read-back). Failed runs write 0%; unreviewed stays null.
Policies: none / score_only / metadata_only / attachments_only / full.
"""

from __future__ import annotations

from typing import Any

from ..errors import ContractError
from ..feedback.overrides import HumanOverrideService
from .attachments import media_extension

VALID_POLICIES = ("none", "score_only", "metadata_only", "attachments_only", "full")


class FeishuSyncService:
    def __init__(
        self,
        client: Any,
        ledger: Any,
        uploader: Any,
        config: dict[str, Any],
        repository: Any,
        artifacts: Any,
        clock: Any = None,
        benchmark: dict[str, Any] | None = None,
    ) -> None:
        self._client = client
        self._ledger = ledger
        self._uploader = uploader
        self._config = config
        self._repository = repository
        self._artifacts = artifacts
        self._clock = clock
        self._dimension_names = {
            str(item.get("dimension_id")): str(item.get("name") or "").strip()
            for item in (benchmark or {}).get("dimensions", [])
            if item.get("dimension_id")
        }
        self._table_structure_checked: set[str] = set()

    def sync_case_run(
        self,
        run: dict[str, Any],
        evaluation: dict[str, Any] | None,
        *,
        policy: str = "full",
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Sync one completed pipeline item without scanning the whole Case table."""

        if policy not in VALID_POLICIES:
            raise ContractError(f"invalid sync policy: {policy!r}")
        if policy == "none":
            return {"action": "skipped", "policy": policy}
        app_token = self._config["base"]["app_token"]
        table_id = self._config["tables"]["case_data"]
        projection = self._config["field_projection"]["case_data"]
        self._read_structure(app_token, table_id)
        record = self._client.find_record(
            app_token,
            table_id,
            projection["case_number"],
            str(run.get("case_number", "")),
            projection["model_version"],
            str(run.get("model_id", "")),
        )
        if policy == "score_only" and record is None:
            raise ContractError(f"score_only cannot create Case for {run.get('case_number', '')}")
        return self._sync_one(
            app_token,
            table_id,
            run,
            evaluation,
            record,
            policy=policy,
            dry_run=dry_run,
        )

    # ------------------------------------------------------------------
    def sync_case_runs(
        self,
        runs: list[dict[str, Any]],
        evaluations: dict[str, dict[str, Any]] | None = None,
        *,
        policy: str = "full",
        dry_run: bool = False,
    ) -> dict[str, Any]:
        if policy not in VALID_POLICIES:
            raise ContractError(f"invalid sync policy: {policy!r}")
        if policy == "none":
            return {"policy": "none", "synced": 0, "skipped": len(runs), "errors": []}

        app_token = self._config["base"]["app_token"]
        table_id = self._config["tables"]["case_data"]
        self._read_structure(app_token, table_id)

        existing = self._read_existing_records(app_token, table_id)
        summary: dict[str, Any] = {
            "policy": policy,
            "created": 0,
            "updated": 0,
            "skipped": 0,
            "errors": [],
            "dry_run": dry_run,
        }
        # EvaluationResult selection is an explicit pipeline boundary. A Run
        # can be judged more than once, so silently taking "latest" would make
        # Case scores depend on timing and can publish an experimental score.
        evaluations = evaluations or {}
        selected_keys: dict[tuple[str, str], str] = {}
        for run in runs:
            key = (str(run.get("case_number", "")), str(run.get("model_id", "")))
            previous_run_id = selected_keys.get(key)
            if previous_run_id and previous_run_id != run.get("run_id"):
                raise ContractError(
                    "sync input contains multiple Runs for the same Feishu Case key "
                    f"{key[0]} + {key[1]}: {previous_run_id}, {run.get('run_id')}"
                )
            selected_keys[key] = str(run.get("run_id", ""))
        for run in runs:
            case_number = run.get("case_number", "")
            model_version = run.get("model_id", "")
            key = (case_number, model_version)
            record = existing.get(key)
            if policy == "score_only" and record is None:
                summary["errors"].append(
                    {
                        "code": "xmax.contract_error",
                        "message": f"score_only cannot create Case for {case_number}",
                        "stage": "sync",
                        "retryable": False,
                        "entity_id": case_number,
                    }
                )
                continue
            try:
                outcome = self._sync_one(
                    app_token,
                    table_id,
                    run,
                    evaluations.get(run.get("run_id")),
                    record,
                    policy=policy,
                    dry_run=dry_run,
                )
                if outcome["action"] == "created":
                    summary["created"] += 1
                elif outcome["action"] == "updated":
                    summary["updated"] += 1
                else:
                    summary["skipped"] += 1
            except Exception as exc:
                summary["errors"].append(
                    {
                        "code": "xmax.external_failure",
                        "message": f"{case_number}: {exc}",
                        "stage": "sync",
                        "retryable": True,
                        "entity_id": case_number,
                    }
                )
        return summary

    # ------------------------------------------------------------------
    def _sync_one(
        self,
        app_token: str,
        table_id: str,
        run: dict[str, Any],
        evaluation: dict[str, Any] | None,
        record: dict[str, Any] | None,
        *,
        policy: str,
        dry_run: bool,
    ) -> dict[str, Any]:
        case_number = run.get("case_number", "")
        model_version = run.get("model_id", "")
        projection = self._config["field_projection"]["case_data"]
        entity_id = f"{case_number}+{model_version}"
        destination = f"feishu:{app_token}/{table_id}"

        score_field = self._score_value(run, evaluation)
        try:
            case = self._repository.get_test_case(run["case_id"])
        except Exception:
            # Compatibility for already-imported legacy runs that predate the
            # frozen TestCase table. New runs always carry a TestCase.
            case = {
                **run,
                "feed_asset_id": run.get("feed_asset_id") or run.get("edited_video_asset_id"),
                "prompt_asset_ids": run.get("prompt_asset_ids", []),
            }
        fields: dict[str, Any] = {
            projection["case_number"]: case_number,
            projection["model_version"]: model_version,
        }
        if policy in {"score_only", "full"}:
            # ``None`` deliberately clears a stale score. It means this Run
            # was synced without an explicitly selected EvaluationResult.
            fields[projection["score_percent"]] = score_field
        if policy in {"score_only", "metadata_only", "full"}:
            description = self._description(run, evaluation)
            fields[projection["description"]] = description
        if policy in {"metadata_only", "full"}:
            fields[projection["prompt_text"]] = case.get("prompt_text", "")

        payload_hash = self._ledger.payload_hash({"fields": fields, "policy": policy})
        if record is not None and policy in {"score_only", "metadata_only", "full"}:
            current = record.get("fields", {})
            metadata_matches = all(current.get(key) == value for key, value in fields.items())
            attachments_complete = (
                self._attachments_complete(record, run, case, projection)
                if policy == "full"
                else True
            )
            if metadata_matches and (policy != "full" or attachments_complete):
                return {"action": "skipped"}

        if dry_run:
            return {"action": "updated" if record else "created", "dry_run": True}

        self._ledger.begin("case_data", entity_id, destination, payload_hash)
        try:
            result = self._client.upsert_record(
                app_token,
                table_id,
                record.get("record_id") if record else None,
                fields,
            )
            record_id = result.get("record_id")
            if not record_id:
                located = self._client.find_record(
                    app_token,
                    table_id,
                    projection["case_number"],
                    case_number,
                    projection["model_version"],
                    model_version,
                )
                record_id = located.get("record_id") if located else None
            if not record_id:
                raise ContractError(
                    f"upsert succeeded but record_id could not be resolved for {entity_id}"
                )
            if policy in {"attachments_only", "full"}:
                self._upload_attachments(
                    app_token,
                    table_id,
                    record_id,
                    run,
                    case,
                    projection,
                    record,
                )
            self._ledger.mark_synced("case_data", entity_id, destination, record_id)
        except Exception as exc:
            self._ledger.mark_error("case_data", entity_id, destination, str(exc))
            raise
        return {"action": "updated" if record else "created", "record_id": record_id}

    # ------------------------------------------------------------------
    def _score_value(self, run: dict[str, Any], evaluation: dict[str, Any] | None) -> float | None:
        if run.get("status") != "completed":
            return float(self._config["case_score"]["failed_run_value"])
        if evaluation is None:
            return None  # unreviewed -> empty
        effective = HumanOverrideService(self._repository).effective_result(evaluation)
        case_score = effective.get("effective_case_score_percent")
        if case_score is None:
            return None
        if run.get("status") != "completed":
            return float(self._config["case_score"]["failed_run_value"])
        internal = float(case_score)
        if internal < 0 or internal > 100:
            raise ContractError(f"case_score_percent out of range: {internal}")
        # internal 0-100 -> feishu 0-1
        return round(internal / 100.0, 4)

    def _description(self, run: dict[str, Any], evaluation: dict[str, Any] | None) -> str | None:
        if run.get("status") == "error":
            return f"生成失败: {run.get('metrics', {}).get('failure_class', 'unknown')}"
        if evaluation:
            dimensions = [
                item
                for item in evaluation.get("dimension_results", [])
                if item.get("assessable") and item.get("score") is not None
            ]
            dimensions.sort(key=lambda item: (float(item["score"]), item.get("dimension_id", "")))
            details: list[str] = []
            for item in dimensions[:2]:
                evidence = next(
                    (
                        str(e.get("description") or "").strip()
                        for e in item.get("evidence", [])
                        if str(e.get("description") or "").strip()
                    ),
                    "",
                )
                if evidence:
                    dimension_id = str(item.get("dimension_id") or "")
                    label = self._dimension_names.get(dimension_id, "")
                    heading = f"{dimension_id} {label}".strip()
                    details.append(f"{heading}——{evidence}")
            if details:
                return ("主要问题：" + "；".join(details))[:1000]
            verdict = str(evaluation.get("final_verdict") or "").strip()
            return verdict[:1000] or None
        # A completed but unreviewed Run has no evaluation description. In
        # particular, never write the meaningless placeholder "已生成".
        return None

    def _upload_attachments(
        self,
        app_token: str,
        table_id: str,
        record_id: str,
        run: dict[str, Any],
        case: dict[str, Any],
        projection: dict[str, Any],
        existing_record: dict[str, Any] | None = None,
    ) -> None:
        existing_fields = (existing_record or {}).get("fields", {})
        for field, specs in self._attachment_specs(run, case, projection).items():
            existing = list(existing_fields.get(field) or [])
            expected_names = [item["filename"] for item in specs]
            kept_names: set[str] = set()
            remove_tokens: list[str] = []
            for item in existing:
                name = str(item.get("name") or "")
                token = item.get("file_token") or item.get("token")
                if name in expected_names and name not in kept_names:
                    kept_names.add(name)
                elif token:
                    remove_tokens.append(str(token))
            if remove_tokens:
                self._client.remove_attachments(
                    app_token, table_id, record_id, field, remove_tokens
                )
            for spec in specs:
                if spec["filename"] in kept_names:
                    continue
                self._uploader.upload(
                    app_token,
                    table_id,
                    record_id,
                    field,
                    spec["uri"],
                    self._artifacts,
                    filename=spec["filename"],
                )

    def _attachments_complete(
        self,
        record: dict[str, Any],
        run: dict[str, Any],
        case: dict[str, Any],
        projection: dict[str, Any],
    ) -> bool:
        fields = record.get("fields", {})
        for field, specs in self._attachment_specs(run, case, projection).items():
            expected = sorted(item["filename"] for item in specs)
            actual = sorted(str(item.get("name") or "") for item in (fields.get(field) or []))
            if actual != expected:
                return False
        return True

    def _attachment_specs(
        self,
        run: dict[str, Any],
        case: dict[str, Any],
        projection: dict[str, Any],
    ) -> dict[str, list[dict[str, str]]]:
        specs: dict[str, list[dict[str, str]]] = {
            projection["result_attachment"]: [],
            projection["feed_attachments"]: [],
            projection["prompt_attachments"]: [],
        }
        result_asset_id = run.get("result_asset_id")
        if result_asset_id:
            asset = self._repository.get_asset(result_asset_id)
            specs[projection["result_attachment"]].append(
                self._asset_spec(asset, run.get("case_number") or "case")
            )

        feed_asset_id = case.get("feed_asset_id")
        if feed_asset_id:
            asset = self._repository.get_asset(feed_asset_id)
            specs[projection["feed_attachments"]].append(
                self._asset_spec(asset, case.get("feed_number") or "feed")
            )
            capture = self._feed_capture_spec(case, asset, run)
            if capture:
                specs[projection["feed_attachments"]].append(capture)

        prompt_number = case.get("prompt_number") or "prompt"
        for index, prompt_asset_id in enumerate(case.get("prompt_asset_ids", []), start=1):
            asset = self._repository.get_asset(prompt_asset_id)
            specs[projection["prompt_attachments"]].append(
                self._asset_spec(asset, f"{prompt_number}_prompt素材_{index:02d}")
            )
        return specs

    def _asset_spec(self, asset: dict[str, Any], stem: str) -> dict[str, str]:
        path = self._artifacts.resolve(asset["uri"])
        return {
            "uri": asset["uri"],
            "filename": f"{stem}{media_extension(asset, path)}",
        }

    def _feed_capture_spec(
        self, case: dict[str, Any], feed: dict[str, Any], run: dict[str, Any]
    ) -> dict[str, str] | None:
        bindings = case.get("api_asset_bindings", {})
        if (
            bindings.get("refImagePath") != "feed_capture"
            and bindings.get("input_media_role") != "feed_capture"
        ):
            return None
        if bindings.get("input_media_role") == "feed_capture":
            capture = run.get("metrics", {}).get("input_capture", {})
            uri = capture.get("uri")
            if not uri:
                raise ContractError(
                    f"actual realtime Feed capture missing for Case {case.get('case_number')}"
                )
            if capture.get("source_asset_id") != feed["asset_id"]:
                raise ContractError(
                    f"realtime Feed capture source mismatch for Case {case.get('case_number')}"
                )
            if capture.get("capture_policy") != bindings.get("capture_frame_policy"):
                raise ContractError(
                    f"realtime Feed capture policy mismatch for Case {case.get('case_number')}"
                )
            self._artifacts.verify(uri, capture.get("sha256"))
        else:
            capture_id = f"{feed['asset_id']}-{str(feed.get('sha256') or '')[:12]}"
            uri = f"artifact://captures/{capture_id}/middle.jpg"
        path = self._artifacts.resolve(uri)
        if not path.is_file():
            raise ContractError(
                f"actual feed capture missing for Case {case.get('case_number')}: {uri}"
            )
        return {
            "uri": uri,
            "filename": f"{case.get('feed_number') or 'feed'}_feed截图.jpg",
        }

    # ------------------------------------------------------------------
    def _read_structure(self, app_token: str, table_id: str) -> None:
        if table_id in self._table_structure_checked:
            return
        fields = self._client.get_fields(app_token, table_id)
        # lark-cli 1.0.80 returns ``name``; older clients/fakes used
        # ``field_name``. Both represent the same live table contract.
        names = {item.get("field_name") or item.get("name") for item in fields}
        projection = self._config["field_projection"]["case_data"]
        missing = [name for name in projection.values() if name not in names]
        if missing:
            raise ContractError(f"feishu table {table_id} missing projected fields: {missing}")
        self._table_structure_checked.add(table_id)

    def _read_existing_records(
        self, app_token: str, table_id: str
    ) -> dict[tuple[str, str], dict[str, Any]]:
        """Key by (case编号, Xmax模型版本) after full pagination."""

        projection = self._config["field_projection"]["case_data"]
        records: dict[tuple[str, str], dict[str, Any]] = {}
        page_token: str | None = None
        while True:
            page = self._client.list_records(app_token, table_id, page_token=page_token)
            for record in page.get("records", []):
                fields = record.get("fields", {})
                key = (
                    str(fields.get(projection["case_number"], "")),
                    str(fields.get(projection["model_version"], "")),
                )
                records[key] = record
            if not page.get("has_more"):
                break
            page_token = page.get("page_token")
            if not page_token:
                raise ContractError(f"table {table_id} has_more without page_token")
        return records
