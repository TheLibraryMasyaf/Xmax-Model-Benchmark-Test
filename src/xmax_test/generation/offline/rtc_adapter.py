"""RTC adapter for the Session + RTC offline backend.

Joins the room returned by the Session API, waits for ready, sends a ``start``
message carrying a local task UID and collects lifecycle events filtered by
that UID (``video_started``, ``video_completed``, ``video_stopped``, ``error``).
"""

from __future__ import annotations

from typing import Any, Protocol


class RtcAdapter(Protocol):
    def join(self, room: dict[str, Any], session_uid: str) -> None: ...

    def wait_ready(self, timeout_s: float = 30.0) -> None: ...

    def send_start(self, task_uid: str, payload: dict[str, Any]) -> None: ...

    def send_heartbeat(self, session_uid: str) -> None: ...

    def collect_events(self, task_uid: str) -> list[dict[str, Any]]: ...

    def close(self) -> None: ...


class FakeRtcAdapter:
    """Scriptable RTC fake: success, join failure, start failure, timeout."""

    def __init__(
        self,
        events: list[dict[str, Any]] | None = None,
        *,
        join_fails: bool = False,
        start_fails: bool = False,
        never_ready: bool = False,
        ready_timeout_s: float = 1.0,
    ) -> None:
        self._events = events if events is not None else [
            {"event": "video_started", "payload": {"taskUid": "T", "ts": 1}},
            {"event": "video_completed", "payload": {"taskUid": "T", "resultUrl": "https://example.invalid/result.mp4"}},
        ]
        self._join_fails = join_fails
        self._start_fails = start_fails
        self._never_ready = never_ready
        self._ready_timeout_s = ready_timeout_s
        self.joined = False
        self.closed = False
        self.started_tasks: list[str] = []
        self.calls: list[str] = []

    def join(self, room: dict[str, Any], session_uid: str) -> None:
        self.calls.append("join")
        if self._join_fails:
            raise ConnectionError("fake rtc join failure")
        self.joined = True

    def wait_ready(self, timeout_s: float = 30.0) -> None:
        self.calls.append("wait_ready")
        if self._never_ready:
            raise TimeoutError("fake rtc ready timeout")

    def send_start(self, task_uid: str, payload: dict[str, Any]) -> None:
        self.calls.append("send_start")
        if self._start_fails:
            raise ConnectionError("fake rtc start failure")
        self.started_tasks.append(task_uid)

    def send_heartbeat(self, session_uid: str) -> None:
        self.calls.append("send_heartbeat")

    def collect_events(self, task_uid: str) -> list[dict[str, Any]]:
        self.calls.append("collect_events")
        return [
            {**item, "payload": dict(item.get("payload", {}))}
            for item in self._events
            if item.get("payload", {}).get("taskUid") in {task_uid, "T"}
        ]

    def close(self) -> None:
        self.calls.append("close")
        self.closed = True
