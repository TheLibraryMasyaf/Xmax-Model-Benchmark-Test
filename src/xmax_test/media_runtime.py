"""Resolve the project-owned ffmpeg/ffprobe runtime.

Production installs include ``static-ffmpeg`` so media execution does not
silently depend on a separately managed system package.  The downloaded
binaries are added to PATH once per process and all existing subprocess-based
adapters can continue to use the standard ``ffmpeg``/``ffprobe`` names.
"""

from __future__ import annotations

from shutil import which


def ensure_media_tools() -> tuple[str | None, str | None]:
    if which("ffmpeg") and which("ffprobe"):
        return which("ffmpeg"), which("ffprobe")
    try:
        import static_ffmpeg

        static_ffmpeg.add_paths(weak=True)
    except (ImportError, OSError, RuntimeError):
        return which("ffmpeg"), which("ffprobe")
    return which("ffmpeg"), which("ffprobe")
