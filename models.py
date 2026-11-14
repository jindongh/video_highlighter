"""Pydantic data models for the basketball video highlighter system.

Defines structured types for video metadata, highlight detection results,
clip extraction parameters, and project configuration.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, Field, computed_field


class VideoInfo(BaseModel):
    """Metadata about a video file on disk."""

    file_path: str = Field(description="Absolute path to the video file.")
    file_name: str = Field(description="Base name of the video file.")
    duration_seconds: float = Field(
        default=0.0,
        alias="duration",
        description="Total duration in seconds.",
    )
    resolution: str | None = Field(
        default=None,
        description="Resolution string, e.g. '1920x1080'.",
    )
    file_size_bytes: int = Field(
        default=0,
        alias="file_size",
        description="File size in bytes.",
    )
    created_at: datetime | None = Field(
        default=None,
        alias="creation_time",
        description="File creation / media creation timestamp.",
    )
    codec: str | None = Field(
        default=None,
        description="Video codec identifier, e.g. 'h264'.",
    )

    model_config = {
        "populate_by_name": True,
        "json_schema_extra": {
            "examples": [
                {
                    "file_path": "/videos/game1.mp4",
                    "file_name": "game1.mp4",
                    "duration_seconds": 3600.0,
                    "resolution": "1920x1080",
                    "file_size_bytes": 2_000_000_000,
                    "created_at": "2026-11-10T14:30:00",
                    "codec": "h264",
                }
            ]
        },
    }

    @property
    def duration(self) -> float:
        return self.duration_seconds

    @property
    def file_size(self) -> int:
        return self.file_size_bytes

    @property
    def creation_time(self) -> datetime | None:
        return self.created_at



class HighlightType(str, Enum):
    """Enum of recognisable basketball highlight categories."""

    SCORE = "score"
    """Scoring play — layup, dunk, three-pointer, etc."""

    STEAL = "steal"
    """Defensive steal."""

    BLOCK = "block"
    """Shot block."""

    ASSIST = "assist"
    """Great pass leading to a score."""

    FASTBREAK = "fastbreak"
    """Fast-break transition play."""

    REBOUND = "rebound"
    """Notable rebound."""

    CELEBRATION = "celebration"
    """Post-play celebration or reaction."""

    OTHER = "other"
    """Any other noteworthy moment."""


class Highlight(BaseModel):
    """A single highlight moment identified within a video."""

    start_time: float = Field(
        description="Start of the highlight in seconds from the beginning of the video.",
    )
    end_time: float = Field(
        description="End of the highlight in seconds from the beginning of the video.",
    )
    highlight_type: HighlightType = Field(
        description="Category of the highlight.",
    )
    description: str = Field(
        description="Human-readable description of what happens in this highlight.",
    )
    excitement_score: int = Field(
        ge=1,
        le=10,
        description="Excitement rating from 1 (routine) to 10 (spectacular).",
    )
    involves_rainier: bool = Field(
        default=True,
        description="Whether Rainier (#39) is directly involved in this highlight play.",
    )


    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "start_time": 124.5,
                    "end_time": 130.0,
                    "highlight_type": "score",
                    "description": "Player drives baseline for a reverse layup.",
                    "excitement_score": 7,
                }
            ]
        }
    }

    @property
    def excitement(self) -> int:
        return self.excitement_score



class VideoAnalysis(BaseModel):
    """Result of analysing a single video for basketball highlights."""

    video_path: str = Field(
        default="",
        alias="file_path",
        description="Path to the video that was analysed.",
    )
    is_basketball: bool = Field(
        default=False,
        description="Whether the video contains basketball content.",
    )
    highlights: list[Highlight] = Field(
        default_factory=list,
        description="Highlights detected in the video.",
    )
    summary: str = Field(
        default="",
        description="Brief description of the video's content.",
    )
    creation_time: datetime | None = Field(
        default=None,
        alias="created_at",
        description="Original EXIF creation/capture timestamp of the video.",
    )
    analyzed_at: datetime = Field(
        default_factory=datetime.now,
        description="Timestamp when the analysis was performed.",
    )

    model_config = {
        "populate_by_name": True,
    }

    @property
    def file_path(self) -> str:
        return self.video_path

    @property
    def source_path(self) -> str:
        return self.video_path

    @property
    def created_at(self) -> datetime | None:
        return self.creation_time



class AnalysisResult(BaseModel):
    """Aggregated analysis across all processed videos."""

    videos: list[VideoAnalysis] = Field(
        default_factory=list,
        description="Per-video analysis results.",
    )
    errors: dict[str, str] = Field(
        default_factory=dict,
        description="Per-video error messages.",
    )
    created_at: datetime = Field(
        default_factory=datetime.now,
        description="Timestamp when this result set was created.",
    )

    model_config = {
        "populate_by_name": True,
    }

    @property
    def analyses(self) -> list[VideoAnalysis]:
        return self.videos

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_highlights(self) -> int:
        """Total number of highlights found across all videos."""
        return sum(len(v.highlights) for v in self.videos)

    def save(self, file_path: str) -> None:
        """Save this analysis result to a JSON file."""
        import json
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(self.model_dump_json(indent=2))

    @classmethod
    def load(cls, file_path: str) -> "AnalysisResult":
        """Load an analysis result from a JSON file."""
        with open(file_path, "r", encoding="utf-8") as f:
            return cls.model_validate_json(f.read())



class HighlightClip(BaseModel):
    """Specification for a clip to be extracted from a source video."""

    source_video: str = Field(
        description="Path to the source video file.",
    )
    start_time: float = Field(
        description="Buffered start time in seconds (may be earlier than the highlight).",
    )
    end_time: float = Field(
        description="Buffered end time in seconds (may be later than the highlight).",
    )
    highlight: Highlight = Field(
        description="The underlying highlight this clip captures.",
    )


class ProjectConfig(BaseModel):
    """User-facing configuration for a highlight-reel project."""

    input_dirs: list[str] = Field(
        description="Directories to scan for video files.",
    )
    output_path: str = Field(
        description="File path for the final highlight-reel video.",
    )
    music_path: str | None = Field(
        default=None,
        description="Optional path to a background music track.",
    )
    date_after: date | None = Field(
        default=None,
        description="Only include videos created after this date.",
    )
    date_before: date | None = Field(
        default=None,
        description="Only include videos created before this date.",
    )
    min_excitement: int = Field(
        default=5,
        ge=1,
        le=10,
        description="Minimum excitement score for a highlight to be included.",
    )
    buffer_seconds: float = Field(
        default=2.0,
        ge=0.0,
        description="Seconds of extra footage to keep before/after each highlight.",
    )
    fade_duration: float = Field(
        default=0.5,
        ge=0.0,
        description="Duration of cross-fade transitions between clips.",
    )
    max_clip_duration: float = Field(
        default=15.0,
        gt=0.0,
        description="Maximum length in seconds for any single clip.",
    )
    sort_by: str = Field(
        default="chronological",
        description="Sort order of clips: 'chronological' (by EXIF time) or 'excitement'.",
    )


    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "input_dirs": ["/videos/season2026"],
                    "output_path": "/output/highlights.mp4",
                    "music_path": "/music/hype.mp3",
                    "date_after": "2026-09-01",
                    "date_before": None,
                    "min_excitement": 6,
                    "buffer_seconds": 2.0,
                    "fade_duration": 0.5,
                    "max_clip_duration": 15.0,
                }
            ]
        }
    }
