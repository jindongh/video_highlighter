#!/usr/bin/env python3
"""Rainier Basketball Video Highlighter CLI.

Automatically scans, analyzes, and cuts basketball highlight reels
from multiple camera sources (Pixel, DJI Action 5, iPhone).
Features SQLite3 persistent highlight storage for single-game,
quarterly/seasonal, and yearly compilation workflows without re-analysis.
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Sequence

import click

from analyzer import analyze_videos
from db import (
    DEFAULT_DB_PATH,
    get_db_connection,
    get_db_stats,
    get_video_analysis,
    import_json_cache_to_db,
    query_db_highlights,
)
from editor import create_highlight_reel
from models import AnalysisResult, ProjectConfig, VideoInfo
from player import generate_html_player
from scanner import scan_multiple_directories


def _parse_date(ctx: click.Context, param: click.Parameter, value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise click.BadParameter(f"Date '{value}' must be in YYYY-MM-DD format.")


@click.command(name="video-highlighter")
@click.option(
    "--input",
    "-i",
    "inputs",
    multiple=True,
    required=False,
    help="Input directory or video file (can specify multiple times).",
)
@click.option(
    "--output",
    "-o",
    default="highlights.mp4",
    show_default=True,
    help="Output path for the generated highlight reel.",
)
@click.option(
    "--music",
    "-m",
    default=None,
    help="Path to background music file (e.g., mp3).",
)
@click.option(
    "--after",
    default=None,
    callback=_parse_date,
    help="Filter videos/highlights created on or after date (YYYY-MM-DD).",
)
@click.option(
    "--before",
    default=None,
    callback=_parse_date,
    help="Filter videos/highlights created on or before date (YYYY-MM-DD).",
)
@click.option(
    "--min-excitement",
    type=click.IntRange(1, 10),
    default=5,
    show_default=True,
    help="Minimum excitement score (1-10) for highlights to include.",
)
@click.option(
    "--buffer",
    "buffer_seconds",
    type=float,
    default=2.0,
    show_default=True,
    help="Seconds of padding to add before and after each highlight.",
)
@click.option(
    "--max-clip",
    "max_clip_duration",
    type=float,
    default=15.0,
    show_default=True,
    help="Maximum duration in seconds for any single extracted clip.",
)
@click.option(
    "--sort",
    "sort_by",
    type=click.Choice(["chronological", "excitement"], case_sensitive=False),
    default="chronological",
    show_default=True,
    help="Clip sequence: 'chronological' (by EXIF game time) or 'excitement' (highest score first).",
)
@click.option(
    "--db",
    "db_path",
    default=DEFAULT_DB_PATH,
    show_default=True,
    help="Path to SQLite3 database file for persistent highlights.",
)
@click.option(
    "--from-db",
    is_flag=True,
    help="Generate highlight reel directly from SQLite database (ideal for season/yearly reels).",
)
@click.option(
    "--db-stats",
    is_flag=True,
    help="Display summary statistics of videos and highlights stored in SQLite.",
)
@click.option(
    "--rainier-only/--all-players",
    default=False,
    show_default=True,
    help="Filter highlights exclusively to Rainier (#39) plays.",
)
@click.option(
    "--cache",
    default=".analysis_cache.json",
    show_default=True,
    help="Path to legacy analysis cache JSON file.",
)
@click.option(
    "--analyze-only",
    is_flag=True,
    help="Only run video analysis and save to SQLite/JSON; do not cut video.",
)
@click.option(
    "--from-analysis",
    default=None,
    help="Skip AI analysis and cut video directly from an existing analysis JSON.",
)
@click.option(
    "--html",
    "export_html",
    is_flag=True,
    help="Generate an interactive zero-processing HTML5 highlight player (highlights.html) with NO video cutting.",
)
@click.option(
    "--api-key",
    envvar="GEMINI_API_KEY",
    help="Google Gemini API Key (or set GEMINI_API_KEY env var).",
)
def main(
    inputs: Sequence[str],
    output: str,
    music: str | None,
    after: date | None,
    before: date | None,
    min_excitement: int,
    buffer_seconds: float,
    max_clip_duration: float,
    sort_by: str,
    db_path: str,
    from_db: bool,
    db_stats: bool,
    rainier_only: bool,
    cache: str,
    analyze_only: bool,
    from_analysis: str | None,
    export_html: bool,
    api_key: str | None,
) -> None:
    """🏀 Rainier Basketball Video Highlighter CLI with SQLite Storage."""
    click.echo("=" * 60)
    click.echo("   🏀 Rainier Basketball Video Highlighter")
    click.echo("=" * 60)

    # Setup API key if provided via CLI flag
    if api_key:
        os.environ["GEMINI_API_KEY"] = api_key

    # Initialize SQLite connection and sync any existing JSON cache
    conn = get_db_connection(db_path)
    if Path(cache).exists():
        import_json_cache_to_db(cache, conn)

    # 1. Action: Display database statistics
    if db_stats:
        stats = get_db_stats(conn)
        click.echo(f"\n📦 SQLite Database Statistics ({db_path}):")
        click.echo(f"  • Total videos stored     : {stats['total_videos']}")
        click.echo(f"  • Basketball game videos  : {stats['basketball_videos']}")
        click.echo(f"  • Total highlight moments : {stats['total_highlights']}")
        click.echo(f"  • Rainier (#39) highlights: {stats['rainier_highlights']}")
        if stats['earliest_video'] and stats['latest_video']:
            click.echo(f"  • Date range covered      : {stats['earliest_video'][:10]} ~ {stats['latest_video'][:10]}")
        click.echo()
        sys.exit(0)

    # 2. Action: Generate reel directly from SQLite database (Season / Year / Match query)
    if from_db:
        click.echo(f"📂 Querying highlights directly from SQLite ({db_path})...")
        if after or before:
            click.echo(f"   📅 Date Range: {after or 'Earliest'} ~ {before or 'Latest'}")
        click.echo(f"   ⭐ Min Excitement: ★ {min_excitement}/10")
        if rainier_only:
            click.echo("   🏀 Filter: Exclusively Rainier (#39) plays")

        analysis = query_db_highlights(
            conn=conn,
            date_after=after,
            date_before=before,
            min_excitement=min_excitement,
            rainier_only=rainier_only,
        )

        if not analysis.videos or analysis.total_highlights == 0:
            click.secho("\n⚠️  No highlights found in SQLite database matching the given criteria.", fg="yellow")
            sys.exit(0)

    # 3. Action: Load from legacy analysis JSON
    elif from_analysis:
        analysis_path = Path(from_analysis)
        if not analysis_path.exists():
            click.secho(f"Error: Analysis file not found: {from_analysis}", fg="red", err=True)
            sys.exit(1)
        click.echo(f"📂 Loading existing analysis from: {from_analysis}")
        analysis = AnalysisResult.load(str(analysis_path))

    # 4. Action: Scan input directories and analyze any new videos
    else:
        if not inputs:
            click.secho(
                "Error: Please provide --input directories, or use --from-db to compile from SQLite, or --db-stats to view library.",
                fg="red",
                err=True,
            )
            sys.exit(1)

        clean_dirs = [str(Path(d).expanduser().resolve()) for d in inputs]
        click.echo(f"🔍 Scanning directories: {', '.join(clean_dirs)}")

        videos: list[VideoInfo] = scan_multiple_directories(
            directories=clean_dirs,
            date_after=after,
            date_before=before,
        )

        if not videos:
            click.secho("No eligible video files found matching criteria.", fg="yellow")
            sys.exit(0)

        click.echo(f"📹 Found {len(videos)} video file(s) (ordered chronologically by EXIF time):")
        for i, v in enumerate(videos, 1):
            t_str = v.created_at.strftime("%Y-%m-%d %H:%M:%S") if v.created_at else "No EXIF date"
            click.echo(f"   [{i}] {v.file_name} ({t_str}, {v.duration_seconds:.1f}s)")

        # Check which videos are missing from SQLite
        missing_videos = [v for v in videos if get_video_analysis(v.file_path, conn) is None]
        if missing_videos and not os.environ.get("GEMINI_API_KEY"):
            click.secho(
                f"\n⚠️  Notice: {len(missing_videos)} new video(s) need AI analysis, but GEMINI_API_KEY is not set.\n"
                "Please run: export GEMINI_API_KEY='your-key'\n"
                "Or pass it via: --api-key 'your-key'\n",
                fg="yellow",
                err=True,
            )
            sys.exit(1)

        # AI Analysis (automatically skips cached/SQLite entries)
        click.echo(f"\n🤖 Processing {len(videos)} video(s) with SQLite persistence ({db_path})...")
        cache_file = str(Path(cache).expanduser().resolve())
        analysis = analyze_videos(videos, cache_path=cache_file, db_path=db_path)

        # Save analysis result to json for convenience
        analysis.save(cache_file)

    # Display highlight summary
    basketball_videos = [v for v in analysis.videos if v.is_basketball]
    total_moments = sum(len(v.highlights) for v in analysis.videos)
    click.echo(f"\n📊 Highlight Summary:")
    click.echo(f"  • Source videos involved : {len(analysis.videos)}")
    click.echo(f"  • Basketball games found : {len(basketball_videos)}")
    click.echo(f"  • Highlight moments ready: {total_moments}")

    for idx, v in enumerate(analysis.videos, 1):
        if v.highlights:
            click.echo(f"\n  [{idx}] {Path(v.video_path).name}: {len(v.highlights)} moments")
            for h in v.highlights:
                tag = f"[{h.highlight_type.upper()}]" if hasattr(h.highlight_type, "upper") else f"[{h.highlight_type}]"
                player_tag = " [🏀 #39 RAINIER]" if getattr(h, "involves_rainier", False) else ""
                click.echo(f"      {h.start_time:5.1f}s - {h.end_time:5.1f}s (★ {h.excitement_score}/10) {tag:12s}{player_tag} {h.description}")

    if analyze_only:
        click.secho(f"\n✨ Analysis complete and saved to SQLite ({db_path}). Video editing skipped.", fg="green")
        sys.exit(0)

    if total_moments == 0:
        click.secho("\n⚠️  No highlights identified matching criteria.", fg="yellow")
        sys.exit(0)

    # 5. Export interactive HTML5 Player (Zero ffmpeg / Zero re-encoding overhead)
    if export_html:
        html_file = "highlights.html"
        title = "Rainier (#39) Basketball Highlights" if rainier_only else "Basketball Game Highlights"
        out_html = generate_html_player(analysis, output_html=html_file, title=title)
        click.secho(f"\n🎉 Successfully created interactive HTML5 highlight player: {out_html}", fg="green", bold=True)
        click.echo("👉 Open this file in your browser to watch highlights with automatic downtime skip (0 CPU / 0 ffmpeg)!")
        sys.exit(0)

    # 6. Build Highlight Reel with ffmpeg stream-copy (fast)
    config = ProjectConfig(
        input_dirs=list(inputs) if inputs else [],
        output_path=str(Path(output).expanduser().resolve()),
        music_path=str(Path(music).expanduser().resolve()) if music else None,
        date_after=after,
        date_before=before,
        min_excitement=min_excitement,
        buffer_seconds=buffer_seconds,
        max_clip_duration=max_clip_duration,
        sort_by=sort_by,
    )

    click.echo("\n🎬 Building highlight reel with fast stream-copy...")
    result_path = create_highlight_reel(analysis, config)

    if result_path and Path(result_path).exists():
        click.secho(f"\n🎉 Successfully created highlight reel: {result_path}", fg="green", bold=True)
    else:
        click.secho("\n❌ Failed to generate highlight reel. Check logs above.", fg="red", err=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
