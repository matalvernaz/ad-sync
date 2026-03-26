"""
Webhook server for ad-sync.

Listens for POST /hook requests from Sonarr/Radarr shell wrappers.
Request body is application/x-www-form-urlencoded (curl --data-urlencode).
"""

from __future__ import annotations

import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .audiovault import AudioVaultClient, LoginError
from .config import Config
from .workflow import process_episode, process_movie

logger = logging.getLogger(__name__)

# Prevent concurrent describealign runs (CPU/RAM heavy).
_lock = threading.Lock()

_VIDEO_EXTENSIONS = {".mkv", ".mp4", ".m4v", ".avi", ".ts"}
_EPISODE_RE = re.compile(r"[Ss](\d+)[Ee](\d+)")


def serve(port: int = 8686) -> None:
    server = HTTPServer(("0.0.0.0", port), _HookHandler)
    logger.info("ad-sync webhook server listening on port %d", port)
    server.serve_forever()


class _HookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/hook":
            self.send_response(404)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        env = {k: v[0] for k, v in parse_qs(body.decode()).items()}

        with _lock:
            ok = _dispatch(env)

        self.send_response(200 if ok else 500)
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path != "/retry":
            self.send_response(404)
            self.end_headers()
            return

        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        title = params.get("title", "").strip()
        path_str = params.get("path", "").strip()
        dir_str = params.get("dir", "").strip()
        season_str = params.get("season", "").strip()
        episode_str = params.get("episode", "").strip()
        year_str = params.get("year", "").strip()

        if not title:
            self._respond(400, "Missing required parameter: title")
            return

        # Single-file retry (one episode or one movie).
        if path_str:
            if season_str and episode_str:
                label = f"S{int(season_str):02d}E{int(episode_str):02d} of {title!r}"
                threading.Thread(
                    target=_retry_episode,
                    args=(title, path_str, season_str, episode_str),
                    daemon=True,
                ).start()
            else:
                year_label = f" ({year_str})" if year_str else ""
                label = f"movie {title!r}{year_label}"
                threading.Thread(
                    target=_retry_movie,
                    args=(title, path_str, year_str),
                    daemon=True,
                ).start()
            self._respond(202, f"Accepted — queued {label}, check container logs for progress")
            return

        # Directory retry (whole season or whole show).
        if dir_str:
            scan_dir = Path(dir_str)
            if not scan_dir.is_dir():
                self._respond(400, f"Directory does not exist: {dir_str}")
                return
            season_filter = int(season_str) if season_str else None
            label = f"season {season_filter} of {title!r}" if season_filter else f"all seasons of {title!r}"
            threading.Thread(
                target=_retry_dir,
                args=(title, scan_dir, season_filter),
                daemon=True,
            ).start()
            self._respond(202, f"Accepted — queued {label}, check container logs for progress")
            return

        self._respond(400, "Provide path= (single file) or dir= (season or show directory)")

    def _respond(self, code: int, message: str) -> None:
        body = message.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        logger.info(fmt, *args)


def _dispatch(env: dict[str, str]) -> bool:
    sonarr_event = env.get("sonarr_eventtype", "").lower()
    radarr_event = env.get("radarr_eventtype", "").lower()

    if sonarr_event == "test" or radarr_event == "test":
        logger.info("Test event received — configuration looks good.")
        return True

    try:
        config = Config.from_env()
    except ValueError as exc:
        logger.error("%s", exc)
        return False

    if sonarr_event == "download":
        return _sonarr(config, env)
    if radarr_event == "download":
        return _radarr(config, env)

    logger.error(
        "No recognised event type. Got sonarr_eventtype=%r radarr_eventtype=%r",
        sonarr_event, radarr_event,
    )
    return False


def _sonarr(config: Config, env: dict[str, str]) -> bool:
    series_title = env.get("sonarr_series_title", "").strip()
    season_str = env.get("sonarr_episodefile_seasonnumber", "0").strip()
    episode_str = env.get("sonarr_episodefile_episodenumbers", "1").strip()
    file_path_str = env.get("sonarr_episodefile_path", "").strip()

    if not series_title or not file_path_str:
        logger.error("Missing required Sonarr fields.")
        return False

    video_path = Path(file_path_str)
    if not video_path.is_file():
        logger.error("Video file does not exist: %s", video_path)
        return False

    try:
        season = int(season_str)
        episode = int(episode_str.split(",")[0].strip())
    except ValueError:
        logger.error("Could not parse season/episode: %r / %r", season_str, episode_str)
        return False

    try:
        client = AudioVaultClient(config.email, config.password)
    except LoginError as exc:
        logger.error("AudioVault login failed: %s", exc)
        return False

    return process_episode(client, config, video_path, series_title, season, episode)


def _radarr(config: Config, env: dict[str, str]) -> bool:
    movie_title = env.get("radarr_movie_title", "").strip()
    movie_year = env.get("radarr_movie_year", "").strip()
    file_path_str = env.get("radarr_moviefile_path", "").strip()

    if not movie_title or not file_path_str:
        logger.error("Missing required Radarr fields.")
        return False

    video_path = Path(file_path_str)
    if not video_path.is_file():
        logger.error("Video file does not exist: %s", video_path)
        return False

    try:
        client = AudioVaultClient(config.email, config.password)
    except LoginError as exc:
        logger.error("AudioVault login failed: %s", exc)
        return False

    return process_movie(client, config, video_path, movie_title, movie_year)


# ------------------------------------------------------------------
# Retry helpers (run in background threads)
# ------------------------------------------------------------------

def _retry_episode(title: str, path_str: str, season_str: str, episode_str: str) -> None:
    try:
        config = Config.from_env()
    except ValueError as exc:
        logger.error("%s", exc)
        return

    video_path = Path(path_str)
    if not video_path.is_file():
        logger.error("Video file does not exist: %s", video_path)
        return

    try:
        season = int(season_str)
        episode = int(episode_str)
    except ValueError:
        logger.error("Could not parse season/episode: %r / %r", season_str, episode_str)
        return

    try:
        client = AudioVaultClient(config.email, config.password)
    except LoginError as exc:
        logger.error("AudioVault login failed: %s", exc)
        return

    with _lock:
        process_episode(client, config, video_path, title, season, episode)


def _retry_movie(title: str, path_str: str, year_str: str) -> None:
    try:
        config = Config.from_env()
    except ValueError as exc:
        logger.error("%s", exc)
        return

    video_path = Path(path_str)
    if not video_path.is_file():
        logger.error("Video file does not exist: %s", video_path)
        return

    try:
        client = AudioVaultClient(config.email, config.password)
    except LoginError as exc:
        logger.error("AudioVault login failed: %s", exc)
        return

    with _lock:
        process_movie(client, config, video_path, title, year_str)


def _retry_dir(title: str, scan_dir: Path, season_filter: int | None) -> None:
    try:
        config = Config.from_env()
    except ValueError as exc:
        logger.error("%s", exc)
        return

    try:
        client = AudioVaultClient(config.email, config.password)
    except LoginError as exc:
        logger.error("AudioVault login failed: %s", exc)
        return

    video_files = sorted(
        f for f in scan_dir.rglob("*")
        if f.is_file() and f.suffix.lower() in _VIDEO_EXTENSIONS
    )

    if not video_files:
        logger.warning("No video files found in %s", scan_dir)
        return

    for video_path in video_files:
        m = _EPISODE_RE.search(video_path.name)
        if not m:
            logger.warning("Could not parse SxxExx from %s — skipping", video_path.name)
            continue
        season = int(m.group(1))
        episode = int(m.group(2))
        if season_filter is not None and season != season_filter:
            continue
        with _lock:
            process_episode(client, config, video_path, title, season, episode)
