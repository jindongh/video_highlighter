"""Video editing module using ffmpeg.

Cuts highlight clips from source videos and assembles them into a
highlight reel with optional background music.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from models import AnalysisResult, ProjectConfig



# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def get_video_duration(file_path: str) -> float:
    """Return the duration of a video file in seconds using ffprobe.

    Args:
        file_path: Absolute or relative path to the video file.

    Returns:
        Duration in seconds, or ``0.0`` if the probe fails.
    """
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        file_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return float(result.stdout.strip())
    except (subprocess.CalledProcessError, ValueError) as exc:
        print(f"[editor] Failed to probe duration for {file_path}: {exc}")
        return 0.0


# ---------------------------------------------------------------------------
# Clip extraction
# ---------------------------------------------------------------------------


def extract_clip(
    source_path: str,
    start_time: float,
    end_time: float,
    output_path: str,
    reencode: bool = False,
) -> bool:
    """Extract a clip from *source_path* between *start_time* and *end_time*.

    Defaults to fast stream-copy (``-c copy``), which takes less than 0.5s
    per clip without any CPU re-encoding overhead.

    Args:
        source_path: Path to the source video.
        start_time: Start position in seconds.
        end_time: End position in seconds.
        output_path: Destination path for the extracted clip.
        reencode: If True, re-encodes the video (much slower on weak CPUs).

    Returns:
        ``True`` on success, ``False`` on failure.
    """
    duration = end_time - start_time
    if duration <= 0:
        print(f"[editor] Invalid clip range: {start_time:.2f}–{end_time:.2f}")
        return False

    if not reencode:
        # Fast stream-copy: instantaneous (0.3s), 0% CPU overhead
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(start_time),
            "-to", str(end_time),
            "-i", source_path,
            "-c", "copy",
            output_path,
        ]
    else:
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(start_time),
            "-to", str(end_time),
            "-i", source_path,
            "-c:v", "libx264", "-crf", "26", "-preset", "ultrafast",
            "-c:a", "aac",
            output_path,
        ]

    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True)
        return True
    except subprocess.CalledProcessError as exc:
        print(f"[editor] ✗ ffmpeg failed: {exc.stderr[-500:] if exc.stderr else exc}")
        return False



# ---------------------------------------------------------------------------
# Batch clip preparation
# ---------------------------------------------------------------------------


def prepare_clips(
    analysis: AnalysisResult,
    config: ProjectConfig,
    output_dir: str,
) -> list[str]:
    """Extract highlight clips from every analysed video.

    Highlights are filtered by *config.min_excitement*, sorted by
    excitement score (descending), and each clip is capped at
    *config.max_clip_duration* seconds.

    Args:
        analysis: Aggregated analysis result containing per-video highlights.
        config: Project-level configuration (thresholds, paths, etc.).
        output_dir: Directory where numbered clip files are written.

    Returns:
        List of absolute paths to the successfully extracted clip files.
    """
    os.makedirs(output_dir, exist_ok=True)

    # Collect all highlights across videos, calculating absolute EXIF capture time
    all_highlights: list[tuple[str, object, datetime]] = []
    min_epoch = datetime.min.replace(tzinfo=timezone.utc)

    for video in analysis.videos:
        video_base_time = video.creation_time or min_epoch
        for highlight in video.highlights:
            if highlight.excitement_score >= config.min_excitement:
                # Absolute real-world timestamp of this moment in the game
                abs_time = video_base_time + timedelta(seconds=highlight.start_time)
                all_highlights.append((video.video_path, highlight, abs_time))

    # Sort based on config.sort_by
    if config.sort_by == "chronological":
        # True chronological order by EXIF capture time (game flow)
        all_highlights.sort(key=lambda item: item[2])
        print(f"[editor] ⏱️ Sorting {len(all_highlights)} highlights in chronological EXIF order (game flow)")
    else:
        # Rainier (#39)'s plays first, then by excitement score descending
        all_highlights.sort(
            key=lambda item: (
                1 if getattr(item[1], "involves_rainier", True) else 0,
                getattr(item[1], "excitement_score", 5),
            ),
            reverse=True,
        )
        print(f"[editor] ⭐ Sorting {len(all_highlights)} highlights by excitement score")

    clip_paths: list[str] = []
    for idx, (source_path, highlight, abs_time) in enumerate(all_highlights, start=1):
        # Apply buffer and cap duration.
        start = max(highlight.start_time - config.buffer_seconds, 0.0)
        end = highlight.end_time + config.buffer_seconds

        if (end - start) > config.max_clip_duration:
            end = start + config.max_clip_duration

        clip_name = f"clip_{idx:03d}.mp4"
        clip_path = os.path.join(output_dir, clip_name)

        time_str = abs_time.strftime("%H:%M:%S") if abs_time != min_epoch else f"{start:.1f}s"
        print(f"[editor] Clip #{idx:02d} [{time_str}] {getattr(highlight, 'description', '')[:50]}")

        if extract_clip(source_path, start, end, clip_path):
            clip_paths.append(clip_path)


    print(f"[editor] Prepared {len(clip_paths)}/{len(all_highlights)} clips")
    return clip_paths


# ---------------------------------------------------------------------------
# Concatenation
# ---------------------------------------------------------------------------


def concatenate_clips(clip_paths: list[str], output_path: str) -> bool:
    """Concatenate *clip_paths* into a single video at *output_path*.

    First attempts a fast stream-copy via the ffmpeg concat demuxer.
    If that fails (e.g. mismatched codecs), falls back to a full
    re-encode.

    Args:
        clip_paths: Ordered list of clip file paths.
        output_path: Destination path for the concatenated video.

    Returns:
        ``True`` on success, ``False`` on failure.
    """
    if not clip_paths:
        print("[editor] No clips to concatenate.")
        return False

    # Write the concat file list.
    concat_dir = os.path.dirname(output_path) or "."
    concat_list_path = os.path.join(concat_dir, "_concat_list.txt")
    with open(concat_list_path, "w") as fh:
        for path in clip_paths:
            abs_path = os.path.abspath(path)
            fh.write(f"file '{abs_path}'\n")

    # --- Attempt 1: stream copy -------------------------------------------
    cmd_copy = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", concat_list_path,
        "-c", "copy",
        output_path,
    ]
    print("[editor] Concatenating clips (stream copy)…")
    try:
        subprocess.run(cmd_copy, capture_output=True, text=True, check=True)
        print(f"[editor] ✓ Concatenated (copy): {output_path}")
        return True
    except subprocess.CalledProcessError:
        print("[editor] Stream-copy concat failed; falling back to re-encode…")

    # --- Attempt 2: re-encode ---------------------------------------------
    cmd_encode = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", concat_list_path,
        "-c:v", "libx264", "-crf", "23", "-preset", "medium",
        "-c:a", "aac", "-b:a", "128k",
        output_path,
    ]
    try:
        subprocess.run(cmd_encode, capture_output=True, text=True, check=True)
        print(f"[editor] ✓ Concatenated (re-encode): {output_path}")
        return True
    except subprocess.CalledProcessError as exc:
        print(f"[editor] ✗ Concatenation failed: {exc.stderr[-500:] if exc.stderr else exc}")
        return False
    finally:
        # Clean up the temporary concat list.
        try:
            os.remove(concat_list_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Background music
# ---------------------------------------------------------------------------


def add_background_music(
    video_path: str,
    music_path: str,
    output_path: str,
) -> bool:
    """Overlay looping background music on *video_path*.

    The music volume is lowered to 15 % of the original audio level and
    fades out over the last 3 seconds.  If the music track is shorter
    than the video it will loop automatically.

    Args:
        video_path: Path to the input video.
        music_path: Path to the background music file.
        output_path: Destination path for the mixed output.

    Returns:
        ``True`` on success, ``False`` on failure.
    """
    video_dur = get_video_duration(video_path)
    if video_dur <= 0:
        print("[editor] Cannot determine video duration; skipping music.")
        return False

    fade_out_start = max(video_dur - 3.0, 0)

    filter_complex = (
        f"[1:a]volume=0.15,"
        f"afade=t=out:st={fade_out_start}:d=3[music];"
        f"[0:a][music]amix=inputs=2:duration=first[aout]"
    )

    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-stream_loop", "-1",
        "-i", music_path,
        "-filter_complex", filter_complex,
        "-map", "0:v",
        "-map", "[aout]",
        "-c:v", "copy",
        "-c:a", "aac",
        "-shortest",
        output_path,
    ]

    print(f"[editor] Adding background music → {output_path}")
    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True)
        print("[editor] ✓ Background music added.")
        return True
    except subprocess.CalledProcessError as exc:
        print(f"[editor] ✗ Music overlay failed: {exc.stderr[-500:] if exc.stderr else exc}")
        return False


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def create_highlight_reel(
    analysis: AnalysisResult,
    config: ProjectConfig,
) -> str | None:
    """Build a complete highlight reel from analysed footage.

    Orchestrates clip extraction, concatenation, and optional background
    music mixing.  Intermediate files are written to a temporary
    directory that is cleaned up on completion.

    Args:
        analysis: The full analysis result (all videos + highlights).
        config: Project configuration with thresholds and output settings.

    Returns:
        Absolute path to the final highlight reel, or ``None`` on failure.
    """
    with tempfile.TemporaryDirectory(prefix="highlights_") as tmp_dir:
        clips_dir = os.path.join(tmp_dir, "clips")
        concat_path = os.path.join(tmp_dir, "concat.mp4")

        # 1. Extract individual clips ----------------------------------------
        clip_paths = prepare_clips(analysis, config, clips_dir)
        if not clip_paths:
            print("[editor] No clips met the excitement threshold.")
            return None

        # 2. Concatenate into a single video ----------------------------------
        if not concatenate_clips(clip_paths, concat_path):
            print("[editor] Concatenation failed.")
            return None

        # 3. Optionally add background music ----------------------------------
        output_path = os.path.abspath(config.output_path)
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

        if config.music_path and os.path.isfile(config.music_path):
            if not add_background_music(concat_path, config.music_path, output_path):
                print("[editor] Music overlay failed; saving without music.")
                _copy_file(concat_path, output_path)
        else:
            _copy_file(concat_path, output_path)

    # 4. Print summary --------------------------------------------------------
    duration = get_video_duration(output_path)
    print()
    print("=" * 60)
    print("  Highlight Reel Summary")
    print("=" * 60)
    print(f"  Total clips : {len(clip_paths)}")
    print(f"  Duration    : {duration:.1f}s ({duration / 60:.1f} min)")
    print(f"  Output      : {output_path}")
    print("=" * 60)

    return output_path


# ---------------------------------------------------------------------------
# Internal utilities
# ---------------------------------------------------------------------------


def _copy_file(src: str, dst: str) -> None:
    """Copy *src* to *dst* using a simple binary read/write."""
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        while chunk := fin.read(1024 * 1024):
            fout.write(chunk)
