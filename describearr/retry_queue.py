"""Persistent queue for items skipped due to the AudioVault daily download limit."""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class RetryQueue:
    """
    Persists episodes/movies that couldn't be downloaded because the daily
    limit was reached.  Stored as a JSON list at *state_path*.
    """

    def __init__(self, state_path: Path) -> None:
        self._path = state_path

    def add_episode(self, series_title: str, season: int, episode: int, video_path: str) -> None:
        self._append({
            "type": "episode",
            "series_title": series_title,
            "season": season,
            "episode": episode,
            "video_path": video_path,
        })

    def add_movie(self, movie_title: str, movie_year: str, video_path: str) -> None:
        self._append({
            "type": "movie",
            "movie_title": movie_title,
            "movie_year": movie_year,
            "video_path": video_path,
        })

    def load(self) -> list[dict]:
        if not self._path.exists():
            return []
        try:
            return json.loads(self._path.read_text())
        except (json.JSONDecodeError, ValueError):
            logger.warning("Corrupt retry queue at %s — ignoring.", self._path)
            return []

    def save(self, items: list[dict]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(items, indent=2))

    def clear(self) -> None:
        self._path.unlink(missing_ok=True)

    def _append(self, item: dict) -> None:
        items = self.load()
        key = item.get("video_path", "")
        if any(i.get("video_path") == key for i in items):
            logger.debug("Already in retry queue, skipping: %s", key)
            return
        items.append(item)
        self.save(items)
        logger.info("Queued for retry (%d total): %s", len(items), key)
