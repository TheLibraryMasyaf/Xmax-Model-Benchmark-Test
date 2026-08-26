"""Session + RTC offline task state machine.

``create_session -> join_room -> start_sent -> video_started -> completed |
stopped | error | timeout -> close_session``

Heartbeats and lifecycle-event waiting run in parallel. Failure paths always
attempt to close the session.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from ...errors import ExternalServiceError
from ...time import utc_now

# Stable failure classification used by P.2 batch-report statistics.
FAILURE_CLASSES = (
    "session_failure",
    "rtc_join_failure",
    "start_send_failure",
    "heartbeat_failure",
    "model_error",
    "lifecycle_timeout",
    "download_failure",
    "media_validation_failure",
)


class SessionTaskStateMachine:
    def __init__(
        self,
        session_api: Any,
        rtc: Any,
        *,
        heartbeat_interval_s: float = 5.0,
        lifecycle_timeout_s: float = 300.0,
        clock: Any = None,
    ) -> None:
        self._session_api = session_api
        self._rtc = rtc
        self._heartbeat_interval = heartbeat_interval_s
        self._lifecycle_timeout = lifecycle_timeout_s
        self._clock = clock
        self.events: list[dict[str, Any]] = []

    def run(self, task_uid: str, start_payload: dict[str, Any]) -> dict[str, Any]:
        """Run one task and return its outcome; always closes the session."""

        session_uid: str | None = None
        try:
            self._append("create_session", {})
            session = self._session_api.create_session({"taskUid": task_uid})
            session_uid = session.get("sessionUid")
            self._append("session_created", {"sessionUid": session_uid})

            room = session.get("rtc", {})
            try:
                self._rtc.join(room, session_uid or "")
                self._append("join_room", {"room": room.get("room")})
                self._rtc.wait_ready()
                self._append("room_ready", {})
            except ConnectionError as exc:
                return self._failure("rtc_join_failure", str(exc))
            except TimeoutError as exc:
                return self._failure("rtc_join_failure", f"ready timeout: {exc}")

            try:
                self._rtc.send_start(task_uid, start_payload)
                self._append("start_sent", {"taskUid": task_uid})
            except ConnectionError as exc:
                return self._failure("start_send_failure", str(exc))

            heartbeat_errors: list[str] = []
            stop = threading.Event()

            def heartbeat_loop() -> None:
                while not stop.is_set():
                    stop.wait(self._heartbeat_interval)
                    if stop.is_set():
                        return
                    try:
                        if session_uid:
                            self._session_api.heartbeat(session_uid)
                            self._append("heartbeat", {"sessionUid": session_uid})
                    except ExternalServiceError as exc:
                        heartbeat_errors.append(str(exc))
                        self._append("heartbeat_failed", {"error": str(exc)})

            thread = threading.Thread(target=heartbeat_loop, daemon=True)
            thread.start()
            try:
                return self._wait_lifecycle(task_uid, heartbeat_errors)
            finally:
                stop.set()
                thread.join(timeout=2)
        finally:
            if session_uid:
                try:
                    self._session_api.close_session(session_uid)
                    self._append("close_session", {"sessionUid": session_uid})
                except ExternalServiceError as exc:
                    self._append("close_failed", {"error": str(exc)})
            try:
                self._rtc.close()
            except Exception:
                pass

    def _wait_lifecycle(self, task_uid: str, heartbeat_errors: list[str]) -> dict[str, Any]:
        deadline = time.monotonic() + self._lifecycle_timeout
        saw_started = False
        while time.monotonic() < deadline:
            for item in self._rtc.collect_events(task_uid):
                event = item.get("event")
                payload = item.get("payload", {})
                if event == "video_started":
                    saw_started = True
                    self._append("video_started", {"taskUid": task_uid})
                elif event == "video_completed":
                    self._append("video_completed", {"taskUid": task_uid})
                    return {
                        "status": "completed",
                        "result_url": payload.get("resultUrl"),
                        "events": self.events,
                    }
                elif event == "video_stopped":
                    self._append("video_stopped", {"taskUid": task_uid})
                    return self._failure(
                        "model_error",
                        "video_stopped before completion",
                        result_url=payload.get("resultUrl"),
                    )
                elif event == "error":
                    self._append(
                        "error",
                        {"taskUid": task_uid, "message": payload.get("message")},
                    )
                    return self._failure("model_error", str(payload.get("message", "model error")))
            if heartbeat_errors and not saw_started:
                return self._failure("heartbeat_failure", heartbeat_errors[0])
            time.sleep(0.1)
        return self._failure(
            "lifecycle_timeout", f"no completion within {self._lifecycle_timeout}s"
        )

    def _failure(self, failure_class: str, message: str, **extra: Any) -> dict[str, Any]:
        self._append(f"failed_{failure_class}", {"message": message, **extra})
        return {
            "status": "error",
            "failure_class": failure_class,
            "message": message,
            "events": self.events,
            **extra,
        }

    def _append(self, event: str, payload: dict[str, Any]) -> None:
        self.events.append({"event": event, "timestamp": self._timestamp(), "payload": payload})

    def _timestamp(self) -> str:
        if self._clock is not None:
            return self._clock.now()
        return utc_now()
