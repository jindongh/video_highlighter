"""AI video analysis module using the Gemini Interactions API.

Uploads basketball game videos to Google Gemini, asks the model to identify
exciting highlights, and returns structured results.  Uses the new
``google-genai`` SDK (>= 2.25.0) Interactions API — **not** the legacy
``generateContent`` endpoint.

Typical usage::

    from analyzer import analyze_videos
    from models import VideoInfo

    videos = [VideoInfo(file_path="game1.mp4"), VideoInfo(file_path="game2.mp4")]
    result = analyze_videos(videos, cache_path="cache.json")
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from google import genai

from models import AnalysisResult, Highlight, VideoAnalysis, VideoInfo
from scanner import get_video_info
from db import DEFAULT_DB_PATH, get_db_connection, get_video_analysis, save_video_analysis



# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MODEL = "gemini-3.8-flash"
_MAX_RETRIES = 5
_INITIAL_BACKOFF_S = 2.0
_INTER_REQUEST_DELAY_S = 2.0
_FILE_POLL_INTERVAL_S = 5
_PROXY_THRESHOLD_BYTES = 80 * 1024 * 1024  # 80 MB



# ---------------------------------------------------------------------------
# Prompt & schema helpers
# ---------------------------------------------------------------------------


def _get_analysis_prompt(player_info: str = "Rainier (wearing Jersey #39)") -> str:
    """Return the detailed prompt sent to Gemini for basketball video analysis."""
    return (
        "You are an expert basketball scout and video analyst. Carefully watch the entire "
        "video and perform the following analysis.\n\n"
        "1. **Basketball check** – Determine whether this video contains "
        "basketball-related content (a game, practice, tournament, scrimmage, highlights). "
        "Set `is_basketball` to true or false accordingly.\n\n"
        "2. **Summary** – Write a concise 2-3 sentence summary of the "
        "basketball action shown in the video.\n\n"
        f"3. **TARGET PLAYER (TOP PRIORITY)**:\n"
        f"   - The focal player of this highlight system is **{player_info}**.\n"
        "   - Watch specifically for player **#39 (Rainier)**. Detect every moment where #39:\n"
        "     * Scores (layup, jumpshot, drive, floater, 3-pointer, putback)\n"
        "     * Steals the ball or deflects a pass\n"
        "     * Blocks a shot or plays lockdown defense\n"
        "     * Assists or delivers a great pass\n"
        "     * Grabs an offensive or defensive rebound\n"
        "     * Runs a fastbreak transition\n"
        "     * Reacts or celebrates with teammates\n"
        "   - For every moment involving Rainier (#39), set `involves_rainier` to `true` "
        "and describe Rainier's exact action in the `description`.\n"
        "   - Score plays featuring Rainier (#39) generously on `excitement_score` (7-10).\n\n"
        "4. **Highlight identification** – Identify all exciting moments, giving highest preference "
        "to plays featuring Rainier (#39). For each moment provide:\n"
        "   - `start_time`: when the action begins, in seconds from video start (e.g. 24.5).\n"
        "   - `end_time`: when the play finishes, in seconds from video start (e.g. 31.0).\n"
        "   - `highlight_type`: one of `score`, `steal`, `block`, `assist`, `fastbreak`, `rebound`, `celebration`.\n"
        "   - `description`: brief description specifying what happens and Rainier (#39)'s role.\n"
        "   - `excitement_score`: integer from 1 (routine) to 10 (spectacular).\n"
        "   - `involves_rainier`: boolean indicating if Rainier (#39) is directly involved.\n\n"
        "Return your answer strictly as JSON matching the provided schema. "
        "Do NOT include any text outside the JSON object. All timestamps must be in seconds from the start of the video."
    )


def _get_response_schema() -> dict[str, Any]:
    """Return the JSON Schema dict that constrains Gemini's response."""
    return {
        "type": "object",
        "properties": {
            "is_basketball": {
                "type": "boolean",
                "description": "Whether the video contains basketball content.",
            },
            "summary": {
                "type": "string",
                "description": (
                    "A concise 2-3 sentence summary of the basketball action."
                ),
            },
            "highlights": {
                "type": "array",
                "description": "List of exciting moments found in the video.",
                "items": {
                    "type": "object",
                    "properties": {
                        "start_time": {
                            "type": "number",
                            "description": (
                                "Start of the moment in seconds from video start."
                            ),
                        },
                        "end_time": {
                            "type": "number",
                            "description": (
                                "End of the moment in seconds from video start."
                            ),
                        },
                        "highlight_type": {
                            "type": "string",
                            "enum": [
                                "score",
                                "steal",
                                "block",
                                "assist",
                                "fastbreak",
                                "rebound",
                                "celebration",
                            ],
                            "description": "Category of the highlight.",
                        },
                        "description": {
                            "type": "string",
                            "description": (
                                "Brief description of what happens in the moment."
                            ),
                        },
                        "excitement_score": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 10,
                            "description": (
                                "Excitement rating from 1 (mild) to 10 (explosive)."
                            ),
                        },
                        "involves_rainier": {
                            "type": "boolean",
                            "description": "Whether Rainier (#39) is directly involved in this play.",
                        },
                    },
                    "required": [
                        "start_time",
                        "end_time",
                        "highlight_type",
                        "description",
                        "excitement_score",
                    ],
                },
            },
        },
        "required": ["is_basketball", "summary", "highlights"],
    }



# ---------------------------------------------------------------------------
# Single-video analysis
# ---------------------------------------------------------------------------


def analyze_video(
    file_path: str,
    client: genai.Client | None = None,
) -> VideoAnalysis:
    """Upload a video directly to Gemini and return a structured highlight analysis.

    Uploads the original video file directly via the Files API (no local ffmpeg
    transcoding overhead), polls until ACTIVE, and queries Gemini 3.8 Flash.

    Args:
        file_path: Local path to the video file.
        client: Optional pre-initialised :class:`genai.Client`.

    Returns:
        A populated :class:`VideoAnalysis` instance.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Video file not found: {file_path}")

    if client is None:
        client = genai.Client()

    uploaded_file = None
    try:
        # -- 1. Upload directly (0% CPU usage, no ffmpeg transcode) --
        size_mb = path.stat().st_size / (1024 * 1024)
        print(f"  ↑ Uploading {path.name} ({size_mb:.1f}MB) directly to Gemini …")
        uploaded_file = client.files.upload(file=str(path))

        # -- 2. Wait for Gemini cloud processing --
        print(f"  ⏳ Waiting for Gemini cloud processing for {path.name} …")
        while not uploaded_file.state or uploaded_file.state.name != "ACTIVE":
            time.sleep(_FILE_POLL_INTERVAL_S)
            uploaded_file = client.files.get(name=uploaded_file.name)


        # -- 3. Analyse with retries --
        print(f"  🔍 Analysing {path.name} …")
        interaction = _request_with_backoff(client, uploaded_file)

        # -- 4. Parse --
        text = (interaction.output_text or "").strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()

        raw: dict[str, Any] = json.loads(text)

        highlights = [
            Highlight(
                start_time=float(h["start_time"]),
                end_time=float(h["end_time"]),
                highlight_type=h["highlight_type"],
                description=h["description"],
                excitement_score=int(h["excitement_score"]),
                involves_rainier=bool(h.get("involves_rainier", True)),
            )
            for h in raw.get("highlights", [])
        ]

        # Extract EXIF creation time for chronological sorting
        info = get_video_info(file_path)
        video_creation_time = info.created_at if info else None

        analysis = VideoAnalysis(
            file_path=file_path,
            is_basketball=raw.get("is_basketball", False),
            summary=raw.get("summary", ""),
            highlights=highlights,
            creation_time=video_creation_time,
        )
        print(
            f"  ✅ Found {len(highlights)} highlight(s) in {path.name}"
        )
        return analysis


    finally:
        # -- 5. Clean up remote file on Gemini --
        if uploaded_file is not None:
            try:
                client.files.delete(name=uploaded_file.name)
            except Exception:
                pass  # best-effort cleanup




def _request_with_backoff(
    client: genai.Client,
    uploaded_file: Any,
) -> Any:
    """Send the analysis interaction, retrying on rate-limit errors.

    Uses exponential back-off with jitter.  Non-rate-limit errors are
    re-raised immediately.

    Args:
        client: Initialised Gemini client.
        uploaded_file: A file object returned by ``client.files.upload``.

    Returns:
        The :class:`Interaction` response from Gemini.

    Raises:
        RuntimeError: After exhausting all retries.
    """
    backoff = _INITIAL_BACKOFF_S
    last_exc: Exception | None = None

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            interaction = client.interactions.create(
                model=_MODEL,
                input=[
                    {
                        "type": "video",
                        "uri": uploaded_file.uri,
                        "mime_type": uploaded_file.mime_type,
                    },
                    {
                        "type": "text",
                        "text": _get_analysis_prompt(),
                    },
                ],
                response_format={
                    "type": "text",
                    "mime_type": "application/json",
                    "schema": _get_response_schema(),
                },
            )
            return interaction

        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            exc_str = str(exc).lower()
            if "429" in exc_str or "rate" in exc_str or "resource_exhausted" in exc_str:
                print(
                    f"  ⚠ Rate limited (attempt {attempt}/{_MAX_RETRIES}). "
                    f"Retrying in {backoff:.0f}s …"
                )
                time.sleep(backoff)
                backoff *= 2
            else:
                raise

    raise RuntimeError(
        f"Gemini analysis failed after {_MAX_RETRIES} retries: {last_exc}"
    ) from last_exc


# ---------------------------------------------------------------------------
# Batch analysis
# ---------------------------------------------------------------------------


def _cache_key(file_path: str) -> str:
    """Return a deterministic hash key for a video file path.

    Args:
        file_path: The video file path.

    Returns:
        A hex-digest string suitable for use as a JSON key.
    """
    return hashlib.sha256(file_path.encode()).hexdigest()


def _load_cache(cache_path: str) -> dict[str, dict[str, Any]]:
    """Load the on-disk JSON cache, returning an empty dict on failure.

    Args:
        cache_path: Path to the JSON cache file.

    Returns:
        A dictionary mapping cache keys to serialised analysis dicts.
    """
    try:
        with open(cache_path, "r", encoding="utf-8") as fh:
            return json.load(fh)  # type: ignore[no-any-return]
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_cache(cache_path: str, cache: dict[str, dict[str, Any]]) -> None:
    """Persist the cache dictionary to disk as JSON.

    Args:
        cache_path: Destination file path.
        cache: The cache dictionary to write.
    """
    Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as fh:
        json.dump(cache, fh, indent=2)


def _analysis_to_dict(analysis: VideoAnalysis) -> dict[str, Any]:
    """Serialise a :class:`VideoAnalysis` to a plain dictionary.

    Args:
        analysis: The analysis to serialise.

    Returns:
        A JSON-compatible dictionary.
    """
    return {
        "file_path": analysis.file_path,
        "is_basketball": analysis.is_basketball,
        "summary": analysis.summary,
        "creation_time": analysis.creation_time.isoformat() if analysis.creation_time else None,
        "highlights": [
            {
                "start_time": h.start_time,
                "end_time": h.end_time,
                "highlight_type": h.highlight_type,
                "description": h.description,
                "excitement_score": h.excitement_score,
                "involves_rainier": h.involves_rainier,
            }
            for h in analysis.highlights
        ],
    }


def _dict_to_analysis(data: dict[str, Any]) -> VideoAnalysis:
    """Deserialise a dictionary back into a :class:`VideoAnalysis`.

    Args:
        data: A dictionary previously produced by :func:`_analysis_to_dict`.

    Returns:
        A reconstructed :class:`VideoAnalysis` instance.
    """
    highlights = [
        Highlight(
            start_time=float(h["start_time"]),
            end_time=float(h["end_time"]),
            highlight_type=h["highlight_type"],
            description=h["description"],
            excitement_score=int(h["excitement_score"]),
            involves_rainier=bool(h.get("involves_rainier", True)),
        )
        for h in data.get("highlights", [])
    ]

    c_time = None
    if data.get("creation_time"):
        try:
            c_time = datetime.fromisoformat(data["creation_time"])
        except ValueError:
            pass

    return VideoAnalysis(
        file_path=data["file_path"],
        is_basketball=data.get("is_basketball", False),
        summary=data.get("summary", ""),
        highlights=highlights,
        creation_time=c_time,
    )



def analyze_videos(
    videos: list[VideoInfo],
    cache_path: str | None = None,
    db_path: str = DEFAULT_DB_PATH,
) -> AnalysisResult:
    """Analyse a batch of videos, with SQLite database caching and error isolation.

    Each video is checked in the SQLite database first. If previously analyzed,
    its results and highlights are loaded instantly (0 API calls).
    Newly analyzed videos are automatically stored in SQLite.

    Args:
        videos: List of :class:`VideoInfo` objects to process.
        cache_path: Optional path to a legacy JSON cache file.
        db_path: Path to the SQLite3 database file (default: highlighter.db).

    Returns:
        An :class:`AnalysisResult` containing all successful analyses.
    """
    result = AnalysisResult()
    client = genai.Client()
    conn = get_db_connection(db_path)

    cache: dict[str, dict[str, Any]] = (
        _load_cache(cache_path) if cache_path else {}
    )
    total = len(videos)
    cached_count = 0

    for idx, video in enumerate(videos, start=1):
        label = Path(video.file_path).name
        print(f"\n[{idx}/{total}] Checking {label} …")

        # 1. Check SQLite database first
        db_analysis = get_video_analysis(video.file_path, conn)
        if db_analysis is not None:
            print(
                f"  💾 Found in SQLite database ({len(db_analysis.highlights)} highlights) — skipping AI analysis."
            )
            result.videos.append(db_analysis)
            cached_count += 1
            continue

        # 2. Check JSON cache second (backward compatibility)
        key = _cache_key(video.file_path)
        if key in cache:
            print(f"  💾 Found in JSON cache — saving to SQLite & skipping AI analysis.")
            analysis = _dict_to_analysis(cache[key])
            save_video_analysis(analysis, conn, video_info=video)
            result.videos.append(analysis)
            cached_count += 1
            continue

        # 3. New video: Run AI analysis via Gemini
        try:
            analysis = analyze_video(video.file_path, client=client)
            result.videos.append(analysis)

            # Persist to SQLite immediately
            save_video_analysis(analysis, conn, video_info=video)
            print(f"  💾 Saved analysis to SQLite database: {db_path}")

            # Also persist to JSON cache if provided
            if cache_path is not None:
                cache[key] = _analysis_to_dict(analysis)
                _save_cache(cache_path, cache)

        except Exception as exc:  # noqa: BLE001
            error_msg = f"{type(exc).__name__}: {exc}"
            print(f"  ❌ Error analysing {label}: {error_msg}")
            result.errors[video.file_path] = error_msg

        # Small delay between requests to respect rate limits
        if idx < total:
            time.sleep(_INTER_REQUEST_DELAY_S)

    fresh_count = len(result.videos) - cached_count
    print(
        f"\n{'='*50}"
        f"\nDone — {len(result.videos)} processed "
        f"({fresh_count} new, {cached_count} from SQLite/cache), "
        f"{len(result.errors)} error(s)."
    )
    return result

