"""Frame sampling strategies.

Global uniform sampling must cover the start and end of the media; local
high-FPS windows are built around events as BEFORE/TRANSITION/AFTER clips.
"""

from __future__ import annotations

from typing import Any


def global_samples(
    duration_s: float,
    count: int,
    *,
    cover_edges: bool = True,
    fps: float = 30.0,
) -> list[float]:
    """Return timestamps for ``count`` uniformly spaced samples.

    When ``cover_edges`` the first sample is at 0 and the last at
    ``duration_s - 1/fps`` so start/end frames are never dropped.
    """

    if duration_s <= 0 or count <= 0:
        return []
    if count == 1:
        return [0.0]
    if cover_edges:
        last = max(0.0, duration_s - (1.0 / max(fps, 1.0)))
        step = last / (count - 1)
        return [round(index * step, 3) for index in range(count)]
    step = duration_s / count
    return [round((index + 0.5) * step, 3) for index in range(count)]


def event_window(
    event_time_s: float,
    duration_s: float,
    *,
    before_s: float = 1.0,
    after_s: float = 2.0,
    fps: float = 30.0,
) -> dict[str, Any]:
    """Build a BEFORE/TRANSITION/AFTER window around an event time."""

    start = max(0.0, event_time_s - before_s)
    end = min(duration_s, event_time_s + after_s)
    transition_start = max(start, event_time_s - 0.2)
    transition_end = min(end, event_time_s + 0.5)
    return {
        "event_time_s": round(event_time_s, 3),
        "window_start_s": round(start, 3),
        "window_end_s": round(end, 3),
        "before_s": round(start, 3),
        "transition_start_s": round(transition_start, 3),
        "transition_end_s": round(transition_end, 3),
        "after_s": round(end, 3),
        "sample_count": max(1, int(round((end - start) * fps))),
    }


def local_high_fps_timestamps(
    event_time_s: float,
    duration_s: float,
    *,
    before_s: float = 1.0,
    after_s: float = 2.0,
    fps: float = 30.0,
) -> list[float]:
    """High-rate timestamps within the event window."""

    window = event_window(event_time_s, duration_s, before_s=before_s, after_s=after_s, fps=fps)
    start = window["window_start_s"]
    end = window["window_end_s"]
    count = window["sample_count"]
    return global_samples(end - start, count, cover_edges=True, fps=fps) and [
        round(start + index * (end - start) / max(1, count - 1), 3)
        for index in range(count)
    ]
