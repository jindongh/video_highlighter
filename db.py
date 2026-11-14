"""SQLite3 database storage for video highlights.

Stores video metadata, AI analysis results, and timestamped basketball highlights.
Allows instant highlight reel generation by match, season/quarter, or year
without re-running AI analysis.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from models import AnalysisResult, Highlight, HighlightType, VideoAnalysis, VideoInfo
from scanner import get_video_info

DEFAULT_DB_PATH = "highlighter.db"



def get_db_connection(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Connect to SQLite database and ensure tables and indexes exist."""
    path = Path(db_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")

    with conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path TEXT UNIQUE NOT NULL,
                file_name TEXT,
                file_size INTEGER,
                duration_seconds REAL,
                resolution TEXT,
                codec TEXT,
                creation_time TEXT,
                is_basketball INTEGER DEFAULT 0,
                summary TEXT,
                analyzed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS highlights (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id INTEGER NOT NULL,
                start_time REAL NOT NULL,
                end_time REAL NOT NULL,
                highlight_type TEXT NOT NULL,
                description TEXT,
                excitement_score INTEGER NOT NULL,
                involves_rainier INTEGER DEFAULT 1,
                absolute_time TEXT,
                FOREIGN KEY (video_id) REFERENCES videos(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_videos_creation_time ON videos(creation_time);
            CREATE INDEX IF NOT EXISTS idx_highlights_video_id ON highlights(video_id);
            CREATE INDEX IF NOT EXISTS idx_highlights_absolute_time ON highlights(absolute_time);
            CREATE INDEX IF NOT EXISTS idx_highlights_excitement ON highlights(excitement_score);
            CREATE INDEX IF NOT EXISTS idx_highlights_rainier ON highlights(involves_rainier);
            """
        )

    return conn


def get_video_analysis(file_path: str, conn: sqlite3.Connection) -> VideoAnalysis | None:
    """Retrieve an existing video analysis from the database by file path."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, file_path, file_name, file_size, duration_seconds,
               resolution, codec, creation_time, is_basketball, summary, analyzed_at
        FROM videos
        WHERE file_path = ?
        """,
        (file_path,),
    )
    v_row = cur.fetchone()
    if not v_row:
        return None

    video_id = v_row["id"]
    cur.execute(
        """
        SELECT start_time, end_time, highlight_type, description,
               excitement_score, involves_rainier, absolute_time
        FROM highlights
        WHERE video_id = ?
        ORDER BY start_time ASC
        """,
        (video_id,),
    )
    h_rows = cur.fetchall()

    highlights = [
        Highlight(
            start_time=row["start_time"],
            end_time=row["end_time"],
            highlight_type=row["highlight_type"],
            description=row["description"] or "",
            excitement_score=row["excitement_score"],
            involves_rainier=bool(row["involves_rainier"]),
        )
        for row in h_rows
    ]

    c_time = None
    if v_row["creation_time"]:
        try:
            c_time = datetime.fromisoformat(v_row["creation_time"])
        except ValueError:
            pass

    a_time = datetime.now()
    if v_row["analyzed_at"]:
        try:
            a_time = datetime.fromisoformat(v_row["analyzed_at"])
        except ValueError:
            pass

    return VideoAnalysis(
        file_path=v_row["file_path"],
        is_basketball=bool(v_row["is_basketball"]),
        summary=v_row["summary"] or "",
        highlights=highlights,
        creation_time=c_time,
        analyzed_at=a_time,
    )


def save_video_analysis(
    analysis: VideoAnalysis,
    conn: sqlite3.Connection,
    video_info: VideoInfo | None = None,
) -> int:
    """Save or update video analysis and its highlights in SQLite. Returns video record ID."""
    c_time = analysis.creation_time
    if c_time is None and Path(analysis.video_path).exists():
        v_info = get_video_info(analysis.video_path)
        if v_info and v_info.created_at:
            c_time = v_info.created_at

    c_time_str = c_time.isoformat() if c_time else None
    a_time_str = analysis.analyzed_at.isoformat() if analysis.analyzed_at else datetime.now().isoformat()

    file_name = Path(analysis.video_path).name
    file_size = video_info.file_size_bytes if video_info else (Path(analysis.video_path).stat().st_size if Path(analysis.video_path).exists() else 0)
    duration = video_info.duration_seconds if video_info else 0.0
    resolution = video_info.resolution if video_info else None
    codec = video_info.codec if video_info else None


    with conn:
        cur = conn.cursor()
        # Insert or replace into videos
        cur.execute(
            """
            INSERT INTO videos (
                file_path, file_name, file_size, duration_seconds,
                resolution, codec, creation_time, is_basketball, summary, analyzed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(file_path) DO UPDATE SET
                file_name=excluded.file_name,
                file_size=excluded.file_size,
                duration_seconds=excluded.duration_seconds,
                resolution=excluded.resolution,
                codec=excluded.codec,
                creation_time=excluded.creation_time,
                is_basketball=excluded.is_basketball,
                summary=excluded.summary,
                analyzed_at=excluded.analyzed_at
            RETURNING id;
            """,
            (
                analysis.video_path,
                file_name,
                file_size,
                duration,
                resolution,
                codec,
                c_time_str,
                1 if analysis.is_basketball else 0,
                analysis.summary,
                a_time_str,
            ),
        )
        row = cur.fetchone()
        video_id = row[0]

        # Clear existing highlights for this video to avoid duplicates
        cur.execute("DELETE FROM highlights WHERE video_id = ?", (video_id,))

        # Insert new highlights
        base_time = analysis.creation_time
        for h in analysis.highlights:
            abs_time_str = None
            if base_time:
                abs_time = base_time + timedelta(seconds=h.start_time)
                abs_time_str = abs_time.isoformat()

            cur.execute(
                """
                INSERT INTO highlights (
                    video_id, start_time, end_time, highlight_type,
                    description, excitement_score, involves_rainier, absolute_time
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    video_id,
                    h.start_time,
                    h.end_time,
                    h.highlight_type if isinstance(h.highlight_type, str) else h.highlight_type.value,
                    h.description,
                    h.excitement_score,
                    1 if h.involves_rainier else 0,
                    abs_time_str,
                ),
            )

    return video_id


def query_db_highlights(
    conn: sqlite3.Connection,
    date_after: date | None = None,
    date_before: date | None = None,
    min_excitement: int = 5,
    rainier_only: bool = False,
    file_paths: list[str] | None = None,
) -> AnalysisResult:
    """Query highlights across videos within a date range (game, quarter/season, year).

    Returns an AnalysisResult ready to be cut into a highlight reel.
    """
    cur = conn.cursor()

    conditions: list[str] = ["v.is_basketball = 1", "h.excitement_score >= ?"]
    params: list[Any] = [min_excitement]

    if rainier_only:
        conditions.append("h.involves_rainier = 1")

    if date_after:
        conditions.append("date(v.creation_time) >= date(?)")
        params.append(date_after.isoformat())

    if date_before:
        conditions.append("date(v.creation_time) <= date(?)")
        params.append(date_before.isoformat())

    if file_paths:
        placeholders = ",".join("?" for _ in file_paths)
        conditions.append(f"v.file_path IN ({placeholders})")
        params.extend(file_paths)

    where_clause = " AND ".join(conditions)

    query = f"""
        SELECT
            v.id as video_id, v.file_path, v.file_name, v.creation_time, v.summary,
            h.id as highlight_id, h.start_time, h.end_time, h.highlight_type,
            h.description, h.excitement_score, h.involves_rainier, h.absolute_time
        FROM videos v
        JOIN highlights h ON v.id = h.video_id
        WHERE {where_clause}
        ORDER BY COALESCE(h.absolute_time, v.creation_time, v.file_path) ASC, h.start_time ASC
    """

    cur.execute(query, params)
    rows = cur.fetchall()

    # Group highlights by video
    video_map: dict[str, VideoAnalysis] = {}

    for row in rows:
        v_path = row["file_path"]
        if v_path not in video_map:
            c_time = None
            if row["creation_time"]:
                try:
                    c_time = datetime.fromisoformat(row["creation_time"])
                except ValueError:
                    pass

            video_map[v_path] = VideoAnalysis(
                file_path=v_path,
                is_basketball=True,
                summary=row["summary"] or "",
                highlights=[],
                creation_time=c_time,
            )

        h_obj = Highlight(
            start_time=row["start_time"],
            end_time=row["end_time"],
            highlight_type=row["highlight_type"],
            description=row["description"] or "",
            excitement_score=row["excitement_score"],
            involves_rainier=bool(row["involves_rainier"]),
        )
        video_map[v_path].highlights.append(h_obj)

    return AnalysisResult(videos=list(video_map.values()))


def get_db_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    """Get overall statistics about stored videos and highlights in SQLite."""
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM videos")
    total_videos = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM videos WHERE is_basketball = 1")
    basketball_videos = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM highlights")
    total_highlights = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM highlights WHERE involves_rainier = 1")
    rainier_highlights = cur.fetchone()[0]

    cur.execute("SELECT MIN(creation_time), MAX(creation_time) FROM videos WHERE creation_time IS NOT NULL")
    date_range = cur.fetchone()

    return {
        "total_videos": total_videos,
        "basketball_videos": basketball_videos,
        "total_highlights": total_highlights,
        "rainier_highlights": rainier_highlights,
        "earliest_video": date_range[0] if date_range else None,
        "latest_video": date_range[1] if date_range else None,
    }


def import_json_cache_to_db(json_path: str, conn: sqlite3.Connection) -> int:
    """Import an existing .analysis_cache.json or analysis.json file into SQLite."""
    import json
    p = Path(json_path)
    if not p.exists():
        return 0

    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        print(f"[db] Failed to read {json_path}: {exc}")
        return 0

    count = 0
    # Check format: could be dict of cache hashes or AnalysisResult dict
    if isinstance(data, dict):
        if "videos" in data and isinstance(data["videos"], list):
            # AnalysisResult format
            for v_data in data["videos"]:
                try:
                    analysis = VideoAnalysis.model_validate(v_data)
                    save_video_analysis(analysis, conn)
                    count += 1
                except Exception:
                    pass
        else:
            # Hash-keyed cache format: {"hash": {analysis dict}}
            for key, val in data.items():
                if isinstance(val, dict) and "file_path" in val:
                    try:
                        analysis = VideoAnalysis.model_validate(val)
                        save_video_analysis(analysis, conn)
                        count += 1
                    except Exception:
                        pass

    return count
