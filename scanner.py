"""Video scanner module.

Scans directories for video files and extracts metadata (duration, resolution,
codec, creation time) using ffprobe. Supports recursive scanning, date-range
filtering, deduplication, and chronological sorting.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path


from models import VideoInfo

# Extensions recognised as video files (case-insensitive comparison).
VIDEO_EXTENSIONS: set[str] = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".3gp"}

# Videos shorter than this are silently skipped.
MIN_DURATION_SECONDS: float = 10.0


def get_video_info(file_path: str) -> VideoInfo | None:
    """Extract video metadata from *file_path* using ``ffprobe``.

    Args:
        file_path: Absolute or relative path to a video file.

    Returns:
        A :class:`VideoInfo` instance on success, or ``None`` when the file is
        not a valid video or ``ffprobe`` fails.
    """
    path = Path(file_path).resolve()

    if not path.is_file():
        print(f"[scanner] Skipping – not a file: {path}")
        return None

    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        print("[scanner] ERROR: ffprobe is not installed or not on PATH.")
        return None
    except subprocess.TimeoutExpired:
        print(f"[scanner] ffprobe timed out for: {path}")
        return None

    if result.returncode != 0:
        print(f"[scanner] ffprobe failed (rc={result.returncode}) for: {path}")
        return None

    try:
        probe: dict = json.loads(result.stdout)
    except json.JSONDecodeError:
        print(f"[scanner] Could not parse ffprobe JSON for: {path}")
        return None

    # --- Duration -----------------------------------------------------------
    duration: float | None = None
    fmt = probe.get("format", {})
    if "duration" in fmt:
        try:
            duration = float(fmt["duration"])
        except (ValueError, TypeError):
            pass

    if duration is None:
        for stream in probe.get("streams", []):
            if "duration" in stream:
                try:
                    duration = float(stream["duration"])
                    break
                except (ValueError, TypeError):
                    pass

    if duration is None:
        print(f"[scanner] No duration found for: {path}")
        return None

    # --- Video stream -------------------------------------------------------
    width: int | None = None
    height: int | None = None
    codec: str | None = None

    for stream in probe.get("streams", []):
        if stream.get("codec_type") == "video":
            width = stream.get("width")
            height = stream.get("height")
            codec = stream.get("codec_name")
            break

    resolution: str | None = None
    if width and height:
        resolution = f"{width}x{height}"

    # --- Creation time ------------------------------------------------------
    creation_time: datetime | None = _parse_creation_time(probe, path=path)


    # Fall back to file modification time when metadata has no timestamp.
    if creation_time is None:
        stat = path.stat()
        ts = stat.st_birthtime if hasattr(stat, "st_birthtime") else stat.st_mtime
        creation_time = datetime.fromtimestamp(ts, tz=timezone.utc)

    return VideoInfo(
        file_path=str(path),
        file_name=path.name,
        duration_seconds=duration,
        resolution=resolution,
        codec=codec,
        created_at=creation_time,
        file_size_bytes=path.stat().st_size,
    )



# ---------------------------------------------------------------------------
# Directory scanning
# ---------------------------------------------------------------------------

def scan_directory(
    directory: str,
    date_after: date | None = None,
    date_before: date | None = None,
) -> list[VideoInfo]:
    """Recursively scan *directory* for video files.

    Args:
        directory:   Root directory to scan.
        date_after:  If given, exclude videos created **before** this date.
        date_before: If given, exclude videos created **after** this date.

    Returns:
        A list of :class:`VideoInfo` objects sorted chronologically by
        creation time.
    """
    root = Path(directory).resolve()

    if not root.is_dir():
        print(f"[scanner] Not a valid directory: {root}")
        return []

    print(f"[scanner] Scanning directory: {root}")

    videos: list[VideoInfo] = []
    scanned = 0

    for dirpath, _dirnames, filenames in os.walk(root):
        for fname in filenames:
            fpath = Path(dirpath) / fname

            if fpath.suffix.lower() not in VIDEO_EXTENSIONS:
                continue

            scanned += 1
            if scanned % 10 == 0:
                print(f"[scanner]   … processed {scanned} video files so far")

            info = get_video_info(str(fpath))
            if info is None:
                continue

            # Skip very short clips.
            if info.duration < MIN_DURATION_SECONDS:
                print(
                    f"[scanner] Skipping short video "
                    f"({info.duration:.1f}s): {fpath.name}"
                )
                continue

            # Date-range filter.
            if info.creation_time is not None:
                video_date = info.creation_time.date()
                if date_after and video_date < date_after:
                    continue
                if date_before and video_date > date_before:
                    continue

            videos.append(info)

    videos.sort(key=lambda v: v.creation_time or datetime.min.replace(tzinfo=timezone.utc))

    print(
        f"[scanner] Found {len(videos)} video(s) "
        f"(scanned {scanned} file(s)) in {root}"
    )
    return videos


def scan_multiple_directories(
    directories: list[str],
    date_after: date | None = None,
    date_before: date | None = None,
) -> list[VideoInfo]:
    """Scan several directories and return a deduplicated, sorted list.

    Deduplication is based on the resolved file path so that symlinks or
    repeated entries do not produce duplicates.

    Args:
        directories: List of directory paths to scan.
        date_after:  Optional lower-bound date filter (inclusive).
        date_before: Optional upper-bound date filter (inclusive).

    Returns:
        A chronologically sorted list of unique :class:`VideoInfo` objects.
    """
    seen_paths: set[str] = set()
    combined: list[VideoInfo] = []

    for directory in directories:
        results = scan_directory(directory, date_after=date_after, date_before=date_before)
        for info in results:
            resolved = str(Path(info.file_path).resolve())
            if resolved not in seen_paths:
                seen_paths.add(resolved)
                combined.append(info)

    combined.sort(key=lambda v: v.creation_time or datetime.min.replace(tzinfo=timezone.utc))

    print(f"[scanner] Combined total: {len(combined)} unique video(s) from {len(directories)} director(ies)")
    return combined


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_creation_time(probe: dict, path: Path | None = None) -> datetime | None:
    """Try to extract a creation timestamp from ffprobe metadata or filename.

    Checks:
    1. EXIF / QuickTime format tags (``creation_time``, ``com.apple.quicktime.creationdate``, ``date``)
    2. Stream-level tags
    3. Filename datetime patterns (DJI, Pixel, iPhone, standard timestamps)
    """
    candidates: list[str] = []

    # 1. Format-level tags (Pixel, DJI, iPhone QuickTime)
    fmt_tags = probe.get("format", {}).get("tags", {})
    for tag_key in (
        "creation_time",
        "com.apple.quicktime.creationdate",
        "date",
        "encoded_date",
    ):
        if tag_key in fmt_tags:
            candidates.append(fmt_tags[tag_key])

    # 2. Stream-level tags
    for stream in probe.get("streams", []):
        stream_tags = stream.get("tags", {})
        for tag_key in ("creation_time", "com.apple.quicktime.creationdate", "date"):
            if tag_key in stream_tags:
                candidates.append(stream_tags[tag_key])

    for raw in candidates:
        for fmt in (
            "%Y-%m-%dT%H:%M:%S.%fZ",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S.%f%z",
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S",
        ):
            try:
                dt = datetime.strptime(raw, fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except ValueError:
                continue

    # 3. Filename patterns (e.g. dji_mimo_20261107_092218, PXL_20261107_092218, 20261107_092218)
    if path is not None:
        filename = path.name
        # Match YYYYMMDD_HHMMSS
        m = re.search(r"(\d{4})(\d{2})(\d{2})[_\-](\d{2})(\d{2})(\d{2})", filename)
        if m:
            try:
                y, mth, d, h, mn, s = map(int, m.groups())
                return datetime(y, mth, d, h, mn, s, tzinfo=timezone.utc)
            except ValueError:
                pass

        # Match YYYY-MM-DD_HH-MM-SS or YYYY-MM-DDTHH:MM:SS
        m2 = re.search(r"(\d{4})-(\d{2})-(\d{2})[ _T](\d{2})[:\.\-](\d{2})[:\.\-](\d{2})", filename)
        if m2:
            try:
                y, mth, d, h, mn, s = map(int, m2.groups())
                return datetime(y, mth, d, h, mn, s, tzinfo=timezone.utc)
            except ValueError:
                pass

    return None

