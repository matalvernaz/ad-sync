"""
Webhook server for describarr.

Listens for POST /hook requests from Sonarr/Radarr shell wrappers.
Request body is application/x-www-form-urlencoded (curl --data-urlencode).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

from .audiovault import AudioVaultClient, DailyLimitReached, DownloadLimiter, LoginError
from .config import Config
from .retry_queue import RetryQueue
from .workflow import drain_retry_queue, process_episode, process_movie, _safe_dirname

logger = logging.getLogger(__name__)

# Prevent concurrent describealign runs (CPU/RAM heavy).
_lock = threading.Lock()

# Shared AudioVault session — created once and reused across all requests.
_client: Optional[AudioVaultClient] = None
_client_lock = threading.Lock()

# Shared retry queue.
_retry_queue: Optional[RetryQueue] = None
_retry_queue_lock = threading.Lock()

# Current job being processed (set while _lock is held).
_current_job: Optional[dict] = None


@contextmanager
def _set_current_job(info: dict):
    global _current_job
    _current_job = {"started_at": datetime.now().isoformat(), **info}
    try:
        yield
    finally:
        _current_job = None


def _elapsed(iso_start: str) -> str:
    """Human-readable elapsed time from an ISO datetime string."""
    try:
        delta = datetime.now() - datetime.fromisoformat(iso_start)
        secs = int(delta.total_seconds())
    except Exception:
        return "unknown"
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m {secs % 60}s"
    h, rem = divmod(secs, 3600)
    return f"{h}h {rem // 60}m"


def _get_client(config: Config) -> AudioVaultClient:
    global _client
    with _client_lock:
        if _client is None:
            _client = AudioVaultClient(config.email, config.password)
    return _client


def _get_retry_queue(config: Config) -> RetryQueue:
    global _retry_queue
    with _retry_queue_lock:
        if _retry_queue is None:
            _retry_queue = RetryQueue(config.cache_dir / "retry_queue.json")
    return _retry_queue

_VIDEO_EXTENSIONS = {".mkv", ".mp4", ".m4v", ".avi", ".ts"}
_EPISODE_RE = re.compile(r"[Ss](\d+)[Ee](\d+)")


def serve(port: int = 8686) -> None:
    server = HTTPServer(("0.0.0.0", port), _HookHandler)
    logger.info("describarr webhook server listening on port %d", port)
    threading.Thread(target=_midnight_drain_loop, daemon=True).start()
    server.serve_forever()


def _midnight_drain_loop() -> None:
    """Background thread: drain the retry queue shortly after each midnight."""
    while True:
        now = datetime.now()
        next_run = (now + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
        sleep_secs = (next_run - now).total_seconds()
        logger.info("Retry queue drain scheduled in %.0f seconds.", sleep_secs)
        time.sleep(sleep_secs)
        try:
            config = Config.from_env()
        except ValueError as exc:
            logger.error("Cannot drain retry queue: %s", exc)
            continue
        queue = _get_retry_queue(config)
        if not queue.load():
            continue
        client = _get_client(config)
        with _lock:
            with _set_current_job({"type": "drain", "title": "retry queue drain"}):
                drain_retry_queue(queue, client, config)


class _HookHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}

        if path in ("/", ""):
            self.send_response(302)
            self.send_header("Location", "/status")
            self.end_headers()
        elif path == "/status":
            self._handle_status()
        elif path == "/queue":
            self._handle_queue_get()
        elif path == "/retry":
            self._handle_retry(params)
        else:
            self._respond(404, "Not found.")

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/hook":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            env = {k: v[0] for k, v in parse_qs(body.decode()).items()}
            try:
                with _lock:
                    ok = _dispatch(env)
            except Exception as exc:
                logger.error("Unhandled error processing hook: %s", exc, exc_info=True)
                self.send_response(500)
                self.end_headers()
                return
            self.send_response(200 if ok else 500)
            self.end_headers()
        elif parsed.path == "/drain":
            self._handle_drain()
        else:
            self._respond(404, "Not found.")

    def do_DELETE(self):
        parsed = urlparse(self.path)
        if parsed.path == "/queue":
            self._handle_queue_delete()
        else:
            self._respond(404, "Not found.")

    # ------------------------------------------------------------------
    # Endpoint handlers
    # ------------------------------------------------------------------

    def _handle_status(self) -> None:
        try:
            config = Config.from_env()
        except ValueError as exc:
            self._respond(500, str(exc))
            return

        limiter_state = DownloadLimiter(config.cache_dir / "daily_limit.json")._load()
        today = datetime.now().strftime("%Y-%m-%d")
        if limiter_state.get("date") == today:
            count = limiter_state.get("count", 0)
        else:
            count = 0
        limit = DownloadLimiter.DAILY_LIMIT
        queue = _get_retry_queue(config)
        queued = len(queue.load())
        now = datetime.now()
        next_drain = (now + timedelta(days=1)).replace(
            hour=0, minute=5, second=0, microsecond=0
        )
        data = {
            "date": today,
            "downloads_today": count,
            "limit": limit,
            "remaining": max(0, limit - count),
            "retry_queue": queued,
            "next_drain": next_drain.strftime("%Y-%m-%dT%H:%M:%S"),
            "current_job": _current_job,
        }

        accept = self.headers.get("Accept", "")
        parsed = urlparse(self.path)
        fmt = parse_qs(parsed.query).get("format", [None])[0]
        if fmt == "json" or ("text/html" not in accept and fmt != "html"):
            self._respond_json(200, data)
        else:
            self._respond_html(200, _render_status_html(data))

    def _handle_queue_get(self) -> None:
        try:
            config = Config.from_env()
        except ValueError as exc:
            self._respond(500, str(exc))
            return
        items = _get_retry_queue(config).load()
        self._respond_json(200, items)

    def _handle_queue_delete(self) -> None:
        try:
            config = Config.from_env()
        except ValueError as exc:
            self._respond(500, str(exc))
            return
        queue = _get_retry_queue(config)
        n = len(queue.load())
        queue.clear()
        self._respond(200, f"Cleared {n} item(s) from retry queue.")

    def _handle_drain(self) -> None:
        try:
            config = Config.from_env()
        except ValueError as exc:
            self._respond(500, str(exc))
            return
        queue = _get_retry_queue(config)
        if not queue.load():
            self._respond(200, "Retry queue is empty — nothing to drain.")
            return
        threading.Thread(target=_do_drain, daemon=True).start()
        self._respond(202, "Accepted — draining retry queue in background, check container logs for progress.")

    def _handle_retry(self, params: dict) -> None:
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
                try:
                    s, e = int(season_str), int(episode_str)
                except ValueError:
                    self._respond(400, "season and episode must be integers")
                    return
                label = f"S{s:02d}E{e:02d} of {title!r}"
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
            if season_str:
                try:
                    season_filter = int(season_str)
                except ValueError:
                    self._respond(400, "season must be an integer")
                    return
            else:
                season_filter = None
            label = f"season {season_filter} of {title!r}" if season_filter else f"all seasons of {title!r}"
            threading.Thread(
                target=_retry_dir,
                args=(title, scan_dir, season_filter),
                daemon=True,
            ).start()
            self._respond(202, f"Accepted — queued {label}, check container logs for progress")
            return

        self._respond(400, "Provide path= (single file) or dir= (season or show directory)")

    # ------------------------------------------------------------------
    # Response helpers
    # ------------------------------------------------------------------

    def _respond(self, code: int, message: str) -> None:
        body = message.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _respond_json(self, code: int, data) -> None:
        body = json.dumps(data, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _respond_html(self, code: int, html: str) -> None:
        body = html.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        logger.info(fmt, *args)


def _render_status_html(data: dict) -> str:
    job = data["current_job"]
    if job:
        jtype = job.get("type", "")
        if jtype == "movie":
            year = f" ({job['year']})" if job.get("year") else ""
            job_label = f"{job['title']}{year}"
        elif jtype == "episode":
            job_label = f"{job['title']} S{job['season']:02d}E{job['episode']:02d}"
        else:
            job_label = job.get("title", "unknown")
        elapsed = _elapsed(job["started_at"])
        job_html = f"""
  <div class="card active">
    <h2>Currently converting</h2>
    <div class="value">{job_label}</div>
    <div class="meta">Running for {elapsed}</div>
  </div>"""
    else:
        job_html = """
  <div class="card">
    <h2>Currently converting</h2>
    <div class="value idle">Idle</div>
  </div>"""

    next_drain_dt = datetime.fromisoformat(data["next_drain"])
    next_drain_str = next_drain_dt.strftime("%b %-d at %-I:%M %p")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="30">
  <title>describarr status</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 560px; margin: 2rem auto; padding: 0 1rem; color: #111; }}
    h1 {{ margin-bottom: 0.1rem; }}
    .subtitle {{ color: #666; margin-top: 0; font-size: 0.9rem; }}
    .cards {{ display: grid; grid-template-columns: 1fr 1fr; gap: 0.75rem; margin-top: 1rem; }}
    .card {{ border: 1px solid #ddd; border-radius: 8px; padding: 0.85rem 1rem; }}
    .card.wide {{ grid-column: 1 / -1; }}
    .card.active {{ border-color: #f0a000; background: #fffbec; }}
    h2 {{ margin: 0 0 0.35rem; font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.06em; color: #888; }}
    .value {{ font-size: 1.35rem; font-weight: 600; }}
    .value.idle {{ color: #aaa; font-weight: 400; }}
    .meta {{ color: #888; font-size: 0.8rem; margin-top: 0.2rem; }}
    .footer {{ color: #bbb; font-size: 0.75rem; margin-top: 1.5rem; }}
  </style>
</head>
<body>
  <h1>describarr</h1>
  <p class="subtitle">Audio description sync &mdash; {data['date']}</p>
  <div class="cards">
  <div class="card wide">{job_html.strip()}</div>
  <div class="card">
    <h2>Downloads today</h2>
    <div class="value">{data['downloads_today']} <span style="font-size:1rem;color:#888">/ {data['limit']}</span></div>
    <div class="meta">{data['remaining']} remaining</div>
  </div>
  <div class="card">
    <h2>Retry queue</h2>
    <div class="value">{data['retry_queue']}</div>
    <div class="meta">Next drain: {next_drain_str}</div>
  </div>
  </div>
  <p class="footer">Auto-refreshes every 30 seconds &middot; <a href="/status?format=json">JSON</a></p>
</body>
</html>"""


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

    client = _get_client(config)
    try:
        with _set_current_job({"type": "episode", "title": series_title, "season": season, "episode": episode}):
            return process_episode(client, config, video_path, series_title, season, episode)
    except DailyLimitReached:
        _get_retry_queue(config).add_episode(series_title, season, episode, str(video_path))
        return False


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

    client = _get_client(config)
    try:
        with _set_current_job({"type": "movie", "title": movie_title, "year": movie_year}):
            return process_movie(client, config, video_path, movie_title, movie_year)
    except DailyLimitReached:
        _get_retry_queue(config).add_movie(movie_title, movie_year, str(video_path))
        return False


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

    client = _get_client(config)
    with _lock:
        with _set_current_job({"type": "episode", "title": title, "season": season, "episode": episode}):
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

    client = _get_client(config)
    with _lock:
        with _set_current_job({"type": "movie", "title": title, "year": year_str}):
            process_movie(client, config, video_path, title, year_str)


def _retry_dir(title: str, scan_dir: Path, season_filter: int | None) -> None:
    try:
        config = Config.from_env()
    except ValueError as exc:
        logger.error("%s", exc)
        return

    client = _get_client(config)

    video_files = sorted(
        f for f in scan_dir.rglob("*")
        if f.is_file() and f.suffix.lower() in _VIDEO_EXTENSIONS
    )

    if not video_files:
        logger.warning("No video files found in %s", scan_dir)
        return

    show_cache_dir = config.cache_dir / "shows" / _safe_dirname(title)

    for video_path in video_files:
        m = _EPISODE_RE.search(video_path.name)
        if not m:
            logger.warning("Could not parse SxxExx from %s — skipping", video_path.name)
            continue
        season = int(m.group(1))
        episode = int(m.group(2))
        if season_filter is not None and season != season_filter:
            continue

        # Skip episodes already successfully processed, so a re-trigger after
        # restart doesn't re-embed the AD track on top of already-merged files.
        done_path = show_cache_dir / f".done_s{season:02d}.json"
        if done_path.exists():
            try:
                done = set(json.loads(done_path.read_text()))
                if episode in done:
                    logger.info(
                        "Skipping S%02dE%02d — already in done list.", season, episode
                    )
                    continue
            except (json.JSONDecodeError, ValueError):
                pass

        with _lock:
            with _set_current_job({"type": "episode", "title": title, "season": season, "episode": episode}):
                process_episode(client, config, video_path, title, season, episode)
