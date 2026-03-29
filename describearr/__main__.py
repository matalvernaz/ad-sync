"""
Entry point for describearr.

Usage (manual):
    python -m describearr              # driven by Sonarr/Radarr environment variables
    python -m describearr --test-auth  # verify AudioVault credentials and exit

Sonarr/Radarr call this script automatically via their Custom Script connection.
Environment variables are set by the arr application before the script is invoked.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from .audiovault import AudioVaultClient, DailyLimitReached, LoginError
from .config import Config
from .retry_queue import RetryQueue
from .workflow import drain_retry_queue, process_episode, process_movie

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("describearr")


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        from .server import serve
        port = int(sys.argv[2]) if len(sys.argv) > 2 else 8686
        serve(port)
        return

    # --test-auth lets users verify credentials without a real media event.
    if "--test-auth" in sys.argv:
        _test_auth()
        return

    try:
        config = Config.from_env()
    except ValueError as exc:
        logger.error("%s", exc)
        sys.exit(1)

    sonarr_event = os.environ.get("sonarr_eventtype", "").lower()
    radarr_event = os.environ.get("radarr_eventtype", "").lower()

    # Both Sonarr and Radarr send a "Test" event when you first configure the
    # connection — respond gracefully so they show a green tick.
    if sonarr_event == "test" or radarr_event == "test":
        logger.info("Test event received — configuration looks good.")
        sys.exit(0)

    try:
        client = AudioVaultClient(config.email, config.password)
    except LoginError as exc:
        logger.error("AudioVault login failed: %s", exc)
        sys.exit(1)

    queue = RetryQueue(config.cache_dir / "retry_queue.json")
    drain_retry_queue(queue, client, config)

    if sonarr_event == "download":
        success = _handle_sonarr(config, client, queue)
    elif radarr_event == "download":
        success = _handle_radarr(config, client, queue)
    else:
        logger.error(
            "No recognised event type in environment. "
            "Expected sonarr_eventtype=Download or radarr_eventtype=Download."
        )
        sys.exit(1)

    sys.exit(0 if success else 1)


# ------------------------------------------------------------------
# Sonarr handler
# ------------------------------------------------------------------

def _handle_sonarr(config: Config, client: AudioVaultClient, queue: RetryQueue) -> bool:
    series_title = os.environ.get("sonarr_series_title", "").strip()
    season_str = os.environ.get("sonarr_episodefile_seasonnumber", "0").strip()
    episode_str = os.environ.get("sonarr_episodefile_episodenumbers", "1").strip()
    file_path_str = os.environ.get("sonarr_episodefile_path", "").strip()

    if not series_title or not file_path_str:
        logger.error("Missing required Sonarr environment variables.")
        return False

    video_path = Path(file_path_str)
    if not video_path.is_file():
        logger.error("Video file does not exist: %s", video_path)
        return False

    try:
        season = int(season_str)
        # Sonarr may list multiple episodes separated by commas; take the first.
        episode = int(episode_str.split(",")[0].strip())
    except ValueError:
        logger.error("Could not parse season/episode numbers: %r / %r", season_str, episode_str)
        return False

    try:
        return process_episode(client, config, video_path, series_title, season, episode)
    except DailyLimitReached:
        queue.add_episode(series_title, season, episode, str(video_path))
        return False


# ------------------------------------------------------------------
# Radarr handler
# ------------------------------------------------------------------

def _handle_radarr(config: Config, client: AudioVaultClient, queue: RetryQueue) -> bool:
    movie_title = os.environ.get("radarr_movie_title", "").strip()
    movie_year = os.environ.get("radarr_movie_year", "").strip()
    file_path_str = os.environ.get("radarr_moviefile_path", "").strip()

    if not movie_title or not file_path_str:
        logger.error("Missing required Radarr environment variables.")
        return False

    video_path = Path(file_path_str)
    if not video_path.is_file():
        logger.error("Video file does not exist: %s", video_path)
        return False

    try:
        return process_movie(client, config, video_path, movie_title, movie_year)
    except DailyLimitReached:
        queue.add_movie(movie_title, movie_year, str(video_path))
        return False


# ------------------------------------------------------------------
# Auth test
# ------------------------------------------------------------------

def _test_auth() -> None:
    try:
        config = Config.from_env()
    except ValueError as exc:
        logger.error("%s", exc)
        sys.exit(1)

    logger.info("Testing AudioVault credentials…")
    try:
        AudioVaultClient(config.email, config.password)
        logger.info("Login successful.")
    except LoginError as exc:
        logger.error("Login failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
