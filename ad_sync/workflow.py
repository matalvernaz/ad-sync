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
import os
import re
import shutil
from pathlib import Path
from typing import Optional

from .aligner import run as align, parse_score, content_score
from .audiovault import AudioVaultClient, DailyLimitReached, DownloadLimiter
from .config import Config
from .matcher import extract_episode, find_movie, find_season

try:
    from . import living_audio as _la
except ImportError:
    _la = None

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
    limiter = DownloadLimiter(config.cache_dir / "daily_limit.json")

    for candidate in candidates:
        try:
            zip_path = _get_cached(client, candidate["url"], zip_cache_dir, limiter)
        except DailyLimitReached:
            return False
        extract_dir = zip_cache_dir / f"season_{season:02d}" / _safe_dirname(candidate["name"])
        audio_path = extract_episode(zip_path, extract_dir, episode)
        if not audio_path:
            logger.warning(
                "E%02d not found in %r — trying next candidate.", episode, candidate["name"]
            )
            continue
        if _align_and_keep(config, video_path, audio_path):
            _mark_episode_done(zip_cache_dir, season, episode, extract_dir, zip_path)
            return True
        logger.info("Candidate %r below threshold — trying next.", candidate["name"])

    if _la is not None:
        client = _la.LivingAudioClient()
        try:
            audio_path = client.find_episode(config.cache_dir, series_title, season, episode)
            if audio_path and _align_and_keep(config, video_path, audio_path):
                return True
        finally:
            client.close()

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
    limiter = DownloadLimiter(config.cache_dir / "daily_limit.json")

    for candidate in candidates:
        try:
            audio_path = _get_cached(client, candidate["url"], movie_cache_dir, limiter)
        except DailyLimitReached:
            return False
        if _align_and_keep(config, video_path, audio_path):
            return True
        logger.info("Candidate %r below threshold — trying next.", candidate["name"])

    if _la is not None:
        client = _la.LivingAudioClient()
        try:
            la_cache = config.cache_dir / "la_movies"
            for candidate in client.search_movies(movie_title, movie_year):
                audio_path = client.download(candidate["url"], la_cache)
                if audio_path and _align_and_keep(config, video_path, audio_path):
                    return True
                logger.info("LivingAudio candidate %r below threshold — trying next.", candidate["name"])
        finally:
            client.close()

    return False


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _align_and_keep(config: Config, video_path: Path, audio_path: Path) -> bool:
    """Run alignment and either keep or discard the combined output."""
    alignment_dir = config.cache_dir / "alignments"
    tmp_output_dir = config.cache_dir / "output"

    combined = align(video_path, audio_path, tmp_output_dir, alignment_dir, config.stretch_audio)
    if combined is None:
        logger.error("Alignment produced no output file.")
        return False

    score = parse_score(video_path, alignment_dir)
    cscore = content_score(video_path, alignment_dir)

    # Accept if either the describealign similarity score clears the threshold
    # OR the content-coverage score does (≥90% of runtime in stable segments).
    # The coverage score rescues episodes where commercial-break seams depress
    # the headline similarity figure even though the alignment is structurally
    # correct (short spike artifacts, 0% rate-change content segments).
    desc_ok = score >= config.min_score
    coverage_ok = cscore >= 90.0

    if not desc_ok and not coverage_ok:
        logger.warning(
            "Score %.1f%% and content coverage %.1f%% both below thresholds — discarding.",
            score, cscore,
        )
        combined.unlink(missing_ok=True)
        return False

    if not desc_ok:
        logger.info(
            "Low similarity score (%.1f%%) but content coverage %.1f%% passes — accepting.",
            score, cscore,
        )

    # Replace the original video with the combined file.
    # shutil.move is non-atomic when source and destination are on different
    # filesystems (e.g. separate Docker volume mounts): it copies then deletes,
    # leaving the destination partially written if the process is killed mid-copy.
    # Instead, copy to a sibling temp file and then os.replace (always atomic on
    # POSIX even across most bind-mount configurations).
    tmp_dest = video_path.parent / (video_path.name + ".tmp")
    try:
        shutil.copy2(combined, tmp_dest)
        os.replace(tmp_dest, video_path)
    except Exception:
        tmp_dest.unlink(missing_ok=True)
        raise
    else:
        combined.unlink(missing_ok=True)
    logger.info("Success (score=%.1f%% coverage=%.1f%%): replaced %s", score, cscore, video_path)
    return True


def _get_cached(
    client: AudioVaultClient,
    url: str,
    cache_dir: Path,
    limiter: Optional[DownloadLimiter] = None,
) -> Path:
    """
    Return a locally cached copy of *url*, downloading if necessary.

    A JSON manifest (manifest.json) in *cache_dir* maps URL → local path so
    that subsequent calls skip the network entirely.

    If *limiter* is provided it is checked (and incremented) before any actual
    HTTP download so we never exceed AudioVault's 25-downloads-per-day cap.
    Cache hits bypass the limiter entirely.
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

    if limiter is not None:
        try:
            limiter.check_and_increment()
        except DailyLimitReached:
            logger.error(
                "Skipping download of %s — AudioVault daily limit reached.", url
            )
            raise

    file_path = client.download(url, cache_dir)

    manifest[url] = str(file_path)
    manifest_path.write_text(json.dumps(manifest, indent=2))

    return file_path


def _safe_dirname(name: str) -> str:
    """Convert an arbitrary string to a safe directory name."""
    name = re.sub(r"[^\w\s-]", "", name).strip()
    return re.sub(r"\s+", "_", name).lower()


_AUDIO_EXTS = {".mp3", ".m4a", ".opus", ".wav", ".aac", ".flac", ".ac3", ".mka"}


def _mark_episode_done(
    zip_cache_dir: Path,
    season: int,
    episode: int,
    extract_dir: Path,
    zip_path: Path,
) -> None:
    """
    Record *episode* as successfully processed for this season.

    When the set of done episodes equals the number of audio files in the
    extracted zip, the zip and its extracted directory are deleted — they're
    no longer needed and just waste disk space.

    The done-episodes file lives at the show level (not inside the season dir)
    so that the zip cache cleanup doesn't erase it.
    """
    season_dir = zip_cache_dir / f"season_{season:02d}"
    progress_path = zip_cache_dir / f".done_s{season:02d}.json"

    done: set[int] = set()
    if progress_path.exists():
        try:
            done = set(json.loads(progress_path.read_text()))
        except (json.JSONDecodeError, ValueError):
            pass

    done.add(episode)
    season_dir.mkdir(parents=True, exist_ok=True)
    progress_path.write_text(json.dumps(sorted(done)))

    # Count how many episodes are in the zip by looking at the extracted dir.
    total = len([
        f for f in extract_dir.rglob("*")
        if f.is_file() and f.suffix.lower() in _AUDIO_EXTS
    ]) if extract_dir.exists() else 0

    if total > 0 and len(done) >= total:
        logger.info(
            "All %d episode(s) of season %d done — clearing zip cache.", total, season
        )
        zip_path.unlink(missing_ok=True)
        # Remove zip from the download manifest.
        manifest_path = zip_cache_dir / "manifest.json"
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text())
                manifest = {k: v for k, v in manifest.items() if Path(v) != zip_path}
                manifest_path.write_text(json.dumps(manifest, indent=2))
            except (json.JSONDecodeError, KeyError):
                pass
        # Delete the extracted dirs and progress file for this season.
        shutil.rmtree(season_dir, ignore_errors=True)
