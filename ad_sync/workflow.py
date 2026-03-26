"""
High-level processing workflows for episodes and movies.

Each function:
  1. Searches AudioVault for the matching audio description.
  2. Downloads and caches the file.
  3. Runs describealign.
  4. Keeps or discards the combined output based on the alignment score.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from pathlib import Path
from typing import Optional

from .aligner import run as align, parse_score
from .audiovault import AudioVaultClient
from .config import Config
from .matcher import extract_episode, find_movie, find_season

logger = logging.getLogger(__name__)


def process_episode(
    client: AudioVaultClient,
    config: Config,
    video_path: Path,
    series_title: str,
    season: int,
    episode: int,
) -> bool:
    """
    Find and align the audio description for a single TV episode.

    Returns True if a combined file was produced with an acceptable score.
    """
    logger.info(
        "Looking up: %s S%02dE%02d", series_title, season, episode
    )

    results = client.search_shows(series_title)
    if not results:
        logger.warning("AudioVault has no results for show: %r", series_title)
        return False

    candidates = find_season(results, series_title, season)
    if not candidates:
        logger.warning("No season %d entry found for %r.", season, series_title)
        return False

    # Season zips are cached by download URL so we only fetch each season once.
    # Each candidate gets its own extract subdirectory so different zips don't
    # overwrite each other's extracted contents.
    zip_cache_dir = config.cache_dir / "shows" / _safe_dirname(series_title)

    for candidate in candidates:
        zip_path = _get_cached(client, candidate["url"], zip_cache_dir)
        extract_dir = zip_cache_dir / f"season_{season:02d}" / _safe_dirname(candidate["name"])
        audio_path = extract_episode(zip_path, extract_dir, episode)
        if not audio_path:
            logger.warning(
                "E%02d not found in %r — trying next candidate.", episode, candidate["name"]
            )
            continue
        if _align_and_keep(config, video_path, audio_path):
            return True
        logger.info("Candidate %r below threshold — trying next.", candidate["name"])

    return False


def process_movie(
    client: AudioVaultClient,
    config: Config,
    video_path: Path,
    movie_title: str,
    movie_year: str,
) -> bool:
    """
    Find and align the audio description for a movie.

    Returns True if a combined file was produced with an acceptable score.
    """
    logger.info("Looking up movie: %s (%s)", movie_title, movie_year)

    results = client.search_movies(movie_title)
    if not results:
        logger.warning("AudioVault has no results for movie: %r", movie_title)
        return False

    candidates = find_movie(results, movie_title, movie_year)
    if not candidates:
        logger.warning("No suitable movie match found for %r.", movie_title)
        return False

    movie_cache_dir = config.cache_dir / "movies"

    for candidate in candidates:
        audio_path = _get_cached(client, candidate["url"], movie_cache_dir)
        if _align_and_keep(config, video_path, audio_path):
            return True
        logger.info("Candidate %r below threshold — trying next.", candidate["name"])

    return False


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _align_and_keep(config: Config, video_path: Path, audio_path: Path) -> bool:
    """Run alignment and either keep or discard the combined output."""
    alignment_dir = config.cache_dir / "alignments"
    tmp_output_dir = config.cache_dir / "output"

    combined = align(video_path, audio_path, tmp_output_dir, alignment_dir)
    if combined is None:
        logger.error("Alignment produced no output file.")
        return False

    score = parse_score(video_path, alignment_dir)

    if score < config.min_score:
        logger.warning(
            "Score %.1f%% is below the %.0f%% threshold — discarding combined file.",
            score,
            config.min_score,
        )
        combined.unlink(missing_ok=True)
        return False

    # Replace the original video with the combined file.
    shutil.move(str(combined), video_path)
    logger.info("Success (%.1f%%): replaced %s", score, video_path)
    return True


def _get_cached(client: AudioVaultClient, url: str, cache_dir: Path) -> Path:
    """
    Return a locally cached copy of *url*, downloading if necessary.

    A JSON manifest (manifest.json) in *cache_dir* maps URL → local path so
    that subsequent calls skip the network entirely.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = cache_dir / "manifest.json"

    manifest: dict[str, str] = {}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text())
        except json.JSONDecodeError:
            logger.warning("Corrupt cache manifest at %s — ignoring.", manifest_path)

    if url in manifest:
        cached = Path(manifest[url])
        if cached.exists():
            logger.info("Cache hit: %s", cached.name)
            return cached
        # Stale entry — file was deleted; fall through to re-download.
        logger.warning("Cached file missing, re-downloading: %s", url)

    file_path = client.download(url, cache_dir)

    manifest[url] = str(file_path)
    manifest_path.write_text(json.dumps(manifest, indent=2))

    return file_path


def _safe_dirname(name: str) -> str:
    """Convert an arbitrary string to a safe directory name."""
    name = re.sub(r"[^\w\s-]", "", name).strip()
    return re.sub(r"\s+", "_", name).lower()
