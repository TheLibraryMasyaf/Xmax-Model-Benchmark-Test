"""Offline generation adapter (REST and Session+RTC backends).

Consumes only the frozen Operation Recipe bindings from the TestCase. The
``refVideoPath`` role (``feed_video`` vs ``prompt_video``) and ``refImagePath``
role (``prompt_image`` vs ``feed_capture``) decide which assets are uploaded
and where they are bound. The audio baseline is the recipe-declared
``expected_audio_source_asset_id``.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

from ...errors import (
    AmbiguousSubmissionError,
    ContractError,
    ExternalServiceError,
    ValidationError,
)
from ...hashing import content_hash, file_sha256
from ...planning.builder import generation_signature
from ...time import utc_now
from ..feed_input import FfmpegFeedPreprocessor
from .repository import OfflineRunRepository
from .rest_adapter import OfflineTaskTransport
from .session_api import SessionApiClient
from .state_machine import SessionTaskStateMachine

TERMINAL_POLL_STATUSES = {"completed", "error", "cancelled"}


class OfflineGenerationAdapter:
    """Unified offline adapter facade over the two documented backends."""

    mode = "offline"

    def __init__(
        self,
        repository: OfflineRunRepository,
        artifacts: Any,
        validator: Any,
        asset_reader: Any,
        *,
        backend: str = "rest",
        transport: OfflineTaskTransport | None = None,
        session_api: SessionApiClient | None = None,
        rtc: Any = None,
        result_source: Any = None,
        model_id: str = "x2.0",
        run_batch_id: str = "run-batch-offline",
        heartbeat_interval_s: float = 5.0,
        lifecycle_timeout_s: float = 300.0,
        capture_extractor: Any = None,
        input_normalizer: Any = None,
        feed_preprocessor: Any = None,
        origin: str = "xmax_offline",
        provider_id: str = "xmax",
        poll_interval_s: float = 0.0,
        max_poll_attempts: int = 300,
        clock: Any = None,
    ) -> None:
        if backend not in {"rest", "session_rtc"}:
            raise ContractError(f"unknown offline backend: {backend}")
        self._repository = repository
        self._artifacts = artifacts
        self._validator = validator
        self._asset_reader = asset_reader
        self._backend = backend
        self._transport = transport
        self._session_api = session_api
        self._rtc = rtc
        self._result_source = result_source
        self.model_id = model_id
        self._run_batch_id = run_batch_id
        self._heartbeat_interval_s = heartbeat_interval_s
        self._lifecycle_timeout_s = lifecycle_timeout_s
        self._capture_extractor = capture_extractor
        self._input_normalizer = input_normalizer
        self._feed_preprocessor = feed_preprocessor or FfmpegFeedPreprocessor(artifacts, validator)
        self._feed_evidence: dict[str, Any] = {}
        self._origin = origin
        self._provider_id = provider_id
        self._poll_interval_s = poll_interval_s
        if isinstance(max_poll_attempts, bool) or not isinstance(max_poll_attempts, int) or max_poll_attempts <= 0:
            raise ContractError("max_poll_attempts must be a positive integer")
        self._max_poll_attempts = max_poll_attempts
        self._clock = clock
        self._upload_cache: dict[str, str] = {}

    # ------------------------------------------------------------------
    # documented unified interface
    # ------------------------------------------------------------------
    def preflight(self) -> dict[str, Any]:
        if self._backend != "rest":
            return {"ok": True, "backend": self._backend}
        checker = getattr(self._transport, "preflight", None)
        if checker is None:
            # Test doubles and custom transports predate this optional hook.
            return {"ok": True, "backend": self._backend, "custom_transport": True}
        return checker()

    def prepare(self, case: dict[str, Any]) -> dict[str, Any]:
        bindings = case.get("api_asset_bindings", {})
        ref_video_role = bindings.get("refVideoPath")
        ref_image_role = bindings.get("refImagePath")
        if ref_video_role not in {"feed_video", "prompt_video"}:
            raise ContractError(
                f"case {case['case_id']} has invalid refVideoPath binding: {ref_video_role!r}"
            )
        assets = self._resolve_assets(case, ref_video_role, ref_image_role)
        normalization_profile = case.get("generation_config", {}).get(
            "input_normalization_profile"
        )
        if normalization_profile:
            if self._input_normalizer is None:
                raise ContractError(
                    f"case {case['case_id']} requires input normalization "
                    f"{normalization_profile!r}, but no normalizer is configured"
                )
            assets["ref_video"] = self._input_normalizer.normalize(
                assets["ref_video"], normalization_profile
            )
        import uuid

        run_id = f"run-{uuid.uuid4().hex[:16]}"
        return {
            "run_id": run_id,
            "case": case,
            "bindings": bindings,
            "assets": assets,
            "feed_preprocessing": self._feed_evidence,
            "backend": self._backend,
        }

    def submit(self, prepared: dict[str, Any]) -> dict[str, Any]:
        case = prepared["case"]
        assets = prepared["assets"]
        task_uid = f"task-{prepared['run_id'][-12:]}"
        public = {role: self._upload_cached(asset) for role, asset in assets.items()}
        payload = {
            "taskUid": task_uid,
            "model": case.get("model_id") or self.model_id,
            "prompt": case.get("prompt_text", ""),
            "refVideoPath": public.get("ref_video"),
            "refImagePath": public.get("ref_image"),
            "audioBaselineAssetId": case.get("expected_audio_source_asset_id"),
        }
        if self._backend == "rest":
            transport = self._transport
            response = transport.submit(payload)
            external_id = response.get("taskUid", task_uid)
            self._repository.append_event(
                prepared["run_id"],
                "task_submitted",
                payload={"external_task_id": external_id},
                external_key=external_id,
            )
            return {
                "external_task_id": external_id,
                "backend": "rest",
                "status": "submitted",
            }
        machine = SessionTaskStateMachine(
            self._session_api,
            self._rtc,
            heartbeat_interval_s=self._heartbeat_interval_s,
            lifecycle_timeout_s=self._lifecycle_timeout_s,
            clock=self._clock,
        )
        outcome = machine.run(task_uid, payload)
        for event in outcome.get("events", []):
            self._repository.append_event(
                prepared["run_id"], event["event"], payload=event["payload"]
            )
        return {
            "external_task_id": task_uid,
            "backend": "session_rtc",
            "status": outcome.get("status", "error"),
            "outcome": outcome,
        }

    def poll(self, task: dict[str, Any]) -> dict[str, Any]:
        if task.get("backend") == "rest":
            state = self._transport.poll(task["external_task_id"])
            terminal = state.get("status") in TERMINAL_POLL_STATUSES
            return {
                "event": f"status_{state.get('status')}",
                "state": state,
                "terminal": terminal,
            }
        return {
            "event": "status_completed",
            "state": task.get("outcome", {}),
            "terminal": True,
        }

    def cancel(self, task: dict[str, Any]) -> None:
        if task.get("backend") == "rest":
            self._repository.append_event(
                task.get("run_id", ""),
                "cancel_requested",
                payload={"external_task_id": task.get("external_task_id")},
            )

    def collect(self, task: dict[str, Any]) -> dict[str, Any]:
        run_id = task.get("run_id", "")
        state = task.get("state", task.get("outcome", {}))
        status = state.get("status", "error")
        metrics: dict[str, Any] = {
            "external_task_id": task.get("external_task_id"),
            "backend": self._backend,
            "submitted_at": utc_now(),
        }
        failure_class = None
        if status == "completed":
            result_url = state.get("result_url") or state.get("resultUrl")
            metrics.update(
                {
                    "result_url": result_url,
                    "credits": state.get("credits"),
                    "billed_seconds": state.get("billed_seconds"),
                }
            )
        else:
            failure_class = state.get("failure_class", "model_error")
            metrics["failure_class"] = failure_class
        return {
            "run_id": run_id,
            "status": status,
            "failure_class": failure_class,
            "metrics": metrics,
            "result_url": state.get("result_url") or state.get("resultUrl"),
        }

    # ------------------------------------------------------------------
    # one-shot convenience used by the CLI
    # ------------------------------------------------------------------
    def run_case(self, case: dict[str, Any]) -> dict[str, Any]:
        """Full cycle: create run, submit, poll to terminal, persist result."""

        started_monotonic = time.monotonic()
        model_id = case.get("model_id") or self.model_id
        inflight = self._repository.inflight_run_for_case(
            case["case_id"], model_id, self._run_batch_id
        )
        if inflight is not None:
            if self._backend != "rest":
                raise ContractError(
                    f"run {inflight['run_id']} is still active and cannot be auto-resumed "
                    f"for backend {self._backend}"
                )
            external_id = self._repository.external_task_id_for_run(inflight["run_id"])
            if not external_id:
                raise ContractError(
                    f"inflight run {inflight['run_id']} has no submitted external task id"
                )
            return self._finish_submitted_run(
                case,
                inflight["run_id"],
                {
                    "run_id": inflight["run_id"],
                    "external_task_id": external_id,
                    "backend": "rest",
                    "status": "submitted",
                },
                started_monotonic,
            )
        prepared = self.prepare(case)
        run_id = prepared["run_id"]
        self._repository.create_run(
            {
                "run_id": run_id,
                "run_batch_id": self._run_batch_id,
                "case_id": case["case_id"],
                "case_number": case["case_number"],
                "status": "running",
                "model_id": case.get("model_id") or self.model_id,
                "mode": "offline",
                "origin": self._origin,
                "provenance": {
                    "source_type": self._origin,
                    "source_locator": self._backend,
                    "source_hash": content_hash(
                        {"case": case["case_id"], "model": case.get("model_id")}
                    ),
                },
                "edited_video_asset_id": case.get("edited_video_asset_id"),
                "expected_audio_source_asset_id": case.get("expected_audio_source_asset_id"),
                "metrics": {
                    "backend": self._backend,
                    "provider": self._provider_id,
                    "generation_signature": case.get("generation_signature")
                    or generation_signature(case),
                },
            }
        )
        self._repository.append_event(
            run_id, "status_running", payload={"case_id": case["case_id"]}
        )
        if prepared.get("feed_preprocessing"):
            self._repository.append_event(run_id, "feed_preprocessed", payload=prepared["feed_preprocessing"])

        try:
            task = self.submit(prepared)
        except Exception as exc:
            if isinstance(exc, AmbiguousSubmissionError):
                self._repository.append_event(
                    run_id,
                    "submission_outcome_unknown",
                    payload={"failure_class": "ambiguous_submission", "error": str(exc)},
                )
                raw_events_uri = self._repository.save_raw_events(
                    run_id, self._repository.get_event_log(run_id)
                )
                self._repository.update_status(
                    run_id,
                    "running",
                    metrics={
                        "backend": self._backend,
                        "provider": self._provider_id,
                        "generation_signature": case.get("generation_signature")
                        or generation_signature(case),
                        "failure_class": "ambiguous_submission",
                        "operator_action": "reconcile provider jobs before retrying",
                    },
                    raw_events_uri=raw_events_uri,
                )
                raise
            self._repository.append_event(
                run_id,
                "status_error",
                payload={"failure_class": "submit_failure", "error": str(exc)},
            )
            self._repository.update_status(
                run_id,
                "error",
                metrics={
                    "backend": self._backend,
                    "provider": self._provider_id,
                    "generation_signature": case.get("generation_signature")
                    or generation_signature(case),
                    "failure_class": "submit_failure",
                    "error": str(exc),
                    "generation_elapsed_s": round(time.monotonic() - started_monotonic, 3),
                    "expected_audio_source_asset_id": case.get("expected_audio_source_asset_id"),
                    "audio_facts": self._audio_facts(case),
                },
            )
            raise
        task["run_id"] = run_id
        task["fresh_run_timing"] = True
        if task.get("backend") == "rest":
            task["submitted_monotonic"] = time.monotonic()

        return self._finish_submitted_run(case, run_id, task, started_monotonic)

    def _finish_submitted_run(
        self,
        case: dict[str, Any],
        run_id: str,
        task: dict[str, Any],
        started_monotonic: float,
    ) -> dict[str, Any]:
        """Poll and persist an already-submitted task without submitting it again."""

        submitted_monotonic = task.get("submitted_monotonic")
        processing_started_monotonic: float | None = None
        terminal_monotonic: float | None = None
        for _ in range(self._max_poll_attempts):
            event = self.poll(task)
            self._repository.append_event(run_id, event["event"], payload={"state": event["state"]})
            task["state"] = event["state"]
            state = event["state"].get("status")
            now = time.monotonic()
            if state == "processing" and processing_started_monotonic is None:
                processing_started_monotonic = now
            if event["terminal"]:
                terminal_monotonic = now
                break
            if self._poll_interval_s > 0:
                time.sleep(self._poll_interval_s)

        if terminal_monotonic is None:
            task["state"] = {
                "status": "error",
                "failure_class": "poll_timeout",
                "error": "offline generation did not reach a terminal state within the poll budget",
            }
            self._repository.append_event(
                run_id,
                "status_error",
                payload={"state": task["state"]},
            )

        collected = self.collect(task)
        status = collected["status"]
        metrics = {
            **collected["metrics"],
            "provider": self._provider_id,
            "generation_elapsed_s": round(time.monotonic() - started_monotonic, 3),
            "generation_signature": case.get("generation_signature") or generation_signature(case),
            "expected_audio_source_asset_id": case.get("expected_audio_source_asset_id"),
            "audio_facts": self._audio_facts(case),
        }
        if (
            isinstance(submitted_monotonic, (int, float))
            and processing_started_monotonic is not None
            and terminal_monotonic is not None
        ):
            metrics["queue_wait_s"] = round(
                processing_started_monotonic - float(submitted_monotonic), 3
            )
            metrics["model_generation_elapsed_s"] = round(
                terminal_monotonic - processing_started_monotonic, 3
            )
        result_asset_id = None
        if status == "completed":
            try:
                result_asset_id, transfer_elapsed_s = self._register_result(
                    case, collected.get("result_url")
                )
                metrics["result_transfer_elapsed_s"] = transfer_elapsed_s
            except (ValidationError, ExternalServiceError) as exc:
                status = "error"
                metrics["failure_class"] = "download_failure"
                metrics["error"] = str(exc)
        if task.get("fresh_run_timing") is True:
            metrics["end_to_end_delivery_elapsed_s"] = round(
                time.monotonic() - started_monotonic, 3
            )
        raw_events_uri = self._repository.save_raw_events(
            run_id, self._repository.get_event_log(run_id)
        )
        self._repository.update_status(
            run_id,
            status,
            metrics=metrics,
            result_asset_id=result_asset_id,
            raw_events_uri=raw_events_uri,
        )
        return self._repository.get_run(run_id)

    # ------------------------------------------------------------------
    def _resolve_assets(
        self, case: dict[str, Any], ref_video_role: str, ref_image_role: str | None
    ) -> dict[str, Any]:
        # Assets are content-addressed.  One byte-identical video may have
        # several record-scoped business bindings (for example a historical
        # Prompt video and a current Feed video).  The frozen Case role is the
        # generation contract; the first physical registry role is provenance,
        # not a reason to reject the same verified media under another video
        # role.
        feed = self._asset_file(case["feed_asset_id"], expected_kind="feed_video")
        feed = self._feed_preprocessor.prepare(feed)
        self._feed_evidence = feed.get("feed_preprocessing", {})
        if ref_video_role == "feed_video" and feed.get("kind") != "feed_video":
            raise ValidationError(
                f"case {case['case_id']} requires a feed video, got {feed.get('kind')}"
            )
        ref_video = (
            feed
            if ref_video_role == "feed_video"
            else self._asset_file(
                case.get("edited_video_asset_id") or "", expected_kind="prompt_video"
            )
        )
        ref_image = None
        if ref_image_role == "prompt_image":
            prompt_images = [
                aid
                for aid in case.get("prompt_asset_ids", [])
                if aid != case.get("edited_video_asset_id")
            ]
            if not prompt_images:
                raise ValidationError(f"case {case['case_id']} requires a prompt image")
            ref_image = self._asset_file(prompt_images[0], expected_kind="prompt_image")
        elif ref_image_role == "feed_capture":
            ref_image = feed if feed.get("kind") == "feed_image" else self._feed_capture(feed)
        return {"ref_video": ref_video, "ref_image": ref_image}

    def _asset_file(
        self, asset_id: str, *, expected_kind: str | None = None
    ) -> dict[str, Any]:
        from ...errors import NotFoundError

        try:
            asset = self._asset_reader.get_asset(asset_id)
        except NotFoundError as exc:
            raise ContractError(str(exc), entity_id=asset_id) from exc
        path = self._artifacts.resolve(asset["uri"])
        if not path.is_file():
            raise ValidationError(f"asset file missing: {asset_id}")
        physical_kind = str(asset.get("kind") or "")
        effective_kind = expected_kind or physical_kind
        if expected_kind is not None:
            expected_family = expected_kind.rsplit("_", 1)[-1]
            physical_family = physical_kind.rsplit("_", 1)[-1]
            mime_family = str(asset.get("mime_type") or "").split("/", 1)[0]
            media = asset.get("media", {})
            media_family = (
                "video"
                if media.get("video_codec") or media.get("fps")
                else "image"
                if media.get("image_format")
                else ""
            )
            if expected_family not in {physical_family, mime_family, media_family}:
                raise ValidationError(
                    f"asset {asset_id} cannot bind as {expected_kind}; "
                    f"physical kind is {physical_kind!r}, mime is {asset.get('mime_type')!r}"
                )
        return {
            "asset_id": asset_id,
            "path": str(path),
            "sha256": asset.get("sha256"),
            "kind": effective_kind,
            "physical_asset_kind": physical_kind,
            "mime_type": asset.get("mime_type"),
            "media": asset.get("media", {}),
        }

    def _feed_capture(self, feed: dict[str, Any]) -> dict[str, Any]:
        if self._capture_extractor is not None:
            return self._capture_extractor(feed)
        from ...media_runtime import ensure_media_tools

        ensure_media_tools()
        capture_id = f"{feed['asset_id']}-{feed.get('sha256', '')[:12]}"
        target = self._artifacts.resolve(f"artifact://captures/{capture_id}/middle.jpg")
        if not target.is_file() or target.stat().st_size == 0:
            target.parent.mkdir(parents=True, exist_ok=True)
            duration = float(feed.get("media", {}).get("duration_s") or 1.0)
            timestamp = max(0.0, duration / 2.0)
            completed = subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-v",
                    "error",
                    "-ss",
                    f"{timestamp:.3f}",
                    "-i",
                    feed["path"],
                    "-frames:v",
                    "1",
                    "-q:v",
                    "2",
                    str(target),
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if completed.returncode != 0 or not target.is_file():
                raise ValidationError(
                    f"feed capture extraction failed for {feed['asset_id']}: "
                    f"{completed.stderr[-500:]}"
                )
        return {
            "asset_id": f"capture:{capture_id}",
            "path": str(target),
            "sha256": file_sha256(target),
            "kind": "feed_image",
            "mime_type": "image/jpeg",
        }

    def _upload_cached(self, asset: dict[str, Any] | None) -> str:
        if asset is None:
            return ""
        sha = asset.get("sha256", "")
        cache_key = f"{asset.get('kind')}:{sha}"
        if cache_key in self._upload_cache:
            return self._upload_cache[cache_key]
        if self._transport is None:
            raise ContractError("offline REST transport is not configured")
        kind = str(asset.get("kind") or "")
        # The hash gives extensionless artifacts a stable SDK File.name analogue;
        # the transport replaces .bin with a detected media extension.
        filename = f"{sha or asset['asset_id']}.bin"
        if kind.endswith("_image") or kind == "feed_image":
            uploaded = self._transport.upload_image(
                asset["path"], filename=filename, mime_type=asset.get("mime_type")
            )
        elif kind.endswith("_video") or kind in {"feed_video", "result_video"}:
            uploaded = self._transport.upload_video(
                asset["path"], filename=filename, mime_type=asset.get("mime_type")
            )
        else:
            raise ContractError(
                f"asset {asset['asset_id']} cannot use an official XMAX upload method: {kind!r}"
            )
        url = uploaded.get("url")
        if not url:
            raise ExternalServiceError(
                f"official XMAX upload returned no URL for {asset['asset_id']}"
            )
        self._upload_cache[cache_key] = url
        return url

    def _register_result(
        self, case: dict[str, Any], result_url: str | None
    ) -> tuple[str, float]:
        if not result_url:
            raise ExternalServiceError("completed task has no result_url")
        target = self._artifacts.resolve(f"artifact://runs/{case['case_id']}/result.mp4")
        target.parent.mkdir(parents=True, exist_ok=True)
        transfer_started = time.monotonic()
        download = self._result_source.fetch(result_url, target)
        transfer_elapsed_s = round(time.monotonic() - transfer_started, 3)
        media = self._validator.validate(target, "result_video")
        return self._register_asset(download, media, case["case_id"]), transfer_elapsed_s

    def _register_asset(self, download: dict[str, Any], media: dict[str, Any], case_id: str) -> str:
        from ...assets.models import DownloadResult

        path = Path(download["path"])
        result = DownloadResult(
            remote_key=f"result-{case_id}",
            path=path,
            sha256=download["sha256"],
            bytes=path.stat().st_size,
            metadata={"kind": "result_video"},
        )
        asset = {
            "asset_id": f"asset_{result.sha256[:16]}",
            "kind": "result_video",
            "uri": f"artifact://assets/asset_{result.sha256[:16]}/source.bin",
            "sha256": result.sha256,
            "bytes": result.bytes,
            "mime_type": "video/mp4",
            "source": {
                "source_id": self._origin,
                "source_kind": "generation",
                "case_id": case_id,
            },
            "status": "ready",
            "media": media,
            "metadata": {"case_id": case_id},
            "created_at": utc_now(),
        }
        # Persist the downloaded file at the declared artifact URI so
        # downstream consumers (attachments, verification) can resolve it.
        self._artifacts.put_file(
            "assets",
            path,
            f"{asset['asset_id']}/source.bin",
            expected_sha256=result.sha256,
        )
        return self._repository.upsert_asset(asset)["asset_id"]

    @staticmethod
    def _audio_facts(case: dict[str, Any]) -> dict[str, Any]:
        return {
            "expected_audio_source_asset_id": case.get("expected_audio_source_asset_id"),
            "baseline_recorded": bool(case.get("expected_audio_source_asset_id")),
        }
