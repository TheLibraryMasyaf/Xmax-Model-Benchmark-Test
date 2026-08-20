"""Offline generation fakes and result sources."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..errors import ExternalServiceError
from ..hashing import file_sha256
from .offline.rest_adapter import FakeOfflineTaskTransport


class FakeResultSource:
    """Fetches a fake result URL to a local file via the transport."""

    def __init__(self, transport: Any) -> None:
        self._transport = transport

    def fetch(self, url: str, destination: Path) -> dict[str, Any]:
        raw = self._transport.download(url, destination)
        return {
            "path": str(destination),
            "sha256": raw["sha256"],
            "bytes": raw["bytes"],
        }


def build_offline_adapter(
    repository: Any,
    artifacts: Any,
    validator: Any,
    asset_reader: Any,
    *,
    backend: str = "rest",
    transport: Any = None,
    session_api: Any = None,
    rtc: Any = None,
    result_bytes: bytes = b"fake-result-video",
    model_id: str = "x2.0",
    run_batch_id: str = "run-batch-fake",
    heartbeat_interval_s: float = 0.05,
    lifecycle_timeout_s: float = 2.0,
    clock: Any = None,
) -> Any:
    """Composition helper returning a fully-fake offline adapter."""

    from .offline.adapter import OfflineGenerationAdapter
    from .offline.rest_adapter import FakeOfflineTaskTransport
    from .offline.rtc_adapter import FakeRtcAdapter
    from .offline.session_api import FakeSessionApiClient

    if transport is None:
        transport = FakeOfflineTaskTransport(result_bytes=result_bytes)
    if session_api is None:
        session_api = FakeSessionApiClient()
    if rtc is None:
        rtc = FakeRtcAdapter()
    result_source = FakeResultSource(transport)
    return OfflineGenerationAdapter(
        repository,
        artifacts,
        validator,
        asset_reader,
        backend=backend,
        transport=transport,
        session_api=session_api,
        rtc=rtc,
        result_source=result_source,
        model_id=model_id,
        run_batch_id=run_batch_id,
        heartbeat_interval_s=heartbeat_interval_s,
        lifecycle_timeout_s=lifecycle_timeout_s,
        capture_extractor=lambda feed: {**feed, "kind": "feed_image"},
        clock=clock,
    )
