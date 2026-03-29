"""
LivingAudio FTP source — private fallback for AudioVault.

Credentials are read from environment variables (loaded via the shared .env):
  LIVINGAUDIO_HOST      (default: paidusers.livingaudio.net)
  LIVINGAUDIO_USER
  LIVINGAUDIO_PASSWORD
"""

from __future__ import annotations

import ftplib
import logging
import os
import re
from pathlib import Path
from typing import Optional

from .matcher import _title_similarity

logger = logging.getLogger(__name__)

_HOST = os.environ.get("LIVINGAUDIO_HOST", "paidusers.livingaudio.net")
_USER = os.environ.get("LIVINGAUDIO_USER", "")
_PASS = os.environ.get("LIVINGAUDIO_PASSWORD", "")

_TV_SUBDIR = "dramas & TV series"
_MOVIE_SUBDIR = "movies"


def _first_letter(title: str) -> str:
    """Return the dvds/ subdirectory for a title (letter or '1-0' for digits)."""
    first = title.strip()[0].upper()
    return "1-0" if first.isdigit() else first


class LivingAudioClient:
    def __init__(self) -> None:
        self._ftp: Optional[ftplib.FTP] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def search_movies(self, title: str, year: str) -> list[dict]:
        """Return scored candidates from the movies folder, best first."""
        letter = _first_letter(title)
        folder = f"/dvds/{letter}/{_MOVIE_SUBDIR}"
        entries = self._listdir(folder)

        title_lower = title.lower()
        scored: list[tuple[float, dict]] = []

        for name, is_dir in entries:
            stem = re.sub(r"\s*\(\d{4}\)\s*$", "", Path(name).stem).strip()
            score = _title_similarity(title_lower, stem.lower())
            if year and f"({year})" in name:
                score += 0.15
            if score < 0.3:
                continue

            remote_path = f"{folder}/{name}"
            if is_dir:
                # Multi-part movie — grab the first MP3 inside.
                sub = self._listdir(remote_path)
                mp3s = sorted(n for n, d in sub if not d and n.lower().endswith(".mp3"))
                if not mp3s:
                    continue
                if len(mp3s) > 1:
                    logger.warning(
                        "LivingAudio: multi-part movie %r has %d parts; "
                        "only part 1 will be aligned.",
                        stem, len(mp3s),
                    )
                remote_path = f"{remote_path}/{mp3s[0]}"
            elif not name.lower().endswith(".mp3"):
                continue  # skip plot.txt etc.

            scored.append((score, {"name": stem, "url": remote_path}))
            logger.info("LivingAudio movie candidate: %r (score %.2f)", stem, score)

        scored.sort(key=lambda x: x[0], reverse=True)
        return [r for _, r in scored]

    def find_episode(
        self, cache_dir: Path, series: str, season: int, episode: int
    ) -> Optional[Path]:
        """Find and download a single episode, returning its local path."""
        letter = _first_letter(series)
        tv_folder = f"/dvds/{letter}/{_TV_SUBDIR}"

        series_dir = self._match_series(tv_folder, series)
        if series_dir is None:
            return None

        safe = re.sub(r"[^\w\s-]", "", series).strip().replace(" ", "_").lower()
        local_dir = cache_dir / "la_shows" / safe
        local_dir.mkdir(parents=True, exist_ok=True)

        remote_dir = f"{tv_folder}/{series_dir}"

        # Try the primary naming convention first (avoids a directory listing).
        primary_name = f"{season}.{episode:02d}.mp3"
        local_primary = local_dir / primary_name
        if local_primary.exists():
            logger.info("LivingAudio cache hit: %s", local_primary.name)
            return local_primary

        result = self._download(f"{remote_dir}/{primary_name}", local_primary)
        if result is not None:
            return result

        # Primary name not found — scan the directory for alternative naming
        # conventions (e.g. S01E05.mp3, Episode 5.mp3, etc.).
        logger.info(
            "LivingAudio: %r not found, scanning %s for alternatives.",
            primary_name, remote_dir,
        )
        remote_path = self._find_episode_remote(remote_dir, season, episode)
        if remote_path is None:
            return None

        alt_name = remote_path.split("/")[-1]
        local_alt = local_dir / alt_name
        if local_alt.exists():
            logger.info("LivingAudio cache hit: %s", local_alt.name)
            return local_alt
        return self._download(remote_path, local_alt)

    def download(self, remote_path: str, cache_dir: Path) -> Optional[Path]:
        """Download a remote FTP path into cache_dir, returning the local path."""
        cache_dir.mkdir(parents=True, exist_ok=True)
        filename = remote_path.split("/")[-1]
        local_path = cache_dir / filename

        if local_path.exists():
            logger.info("LivingAudio cache hit: %s", filename)
            return local_path

        return self._download(remote_path, local_path)

    def close(self) -> None:
        if self._ftp:
            try:
                self._ftp.quit()
            except Exception:
                pass
            self._ftp = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _connect(self) -> ftplib.FTP:
        if self._ftp is None:
            ftp = ftplib.FTP(_HOST)
            ftp.login(_USER, _PASS)
            self._ftp = ftp
        return self._ftp

    def _listdir(self, path: str) -> list[tuple[str, bool]]:
        """Return [(name, is_dir)] for entries in path."""
        ftp = self._connect()
        lines: list[str] = []
        try:
            ftp.dir(path, lines.append)
        except ftplib.error_perm:
            return []

        result = []
        for line in lines:
            parts = line.split(None, 8)
            if len(parts) < 9:
                continue
            result.append((parts[8], line.startswith("d")))
        return result

    def _find_episode_remote(
        self, remote_dir: str, season: int, episode: int
    ) -> Optional[str]:
        """
        Scan *remote_dir* for an MP3 matching *season*/*episode* using common
        naming patterns.  Returns the full remote path, or None.
        """
        entries = self._listdir(remote_dir)
        patterns = [
            re.compile(rf"[Ss]{season:02d}[Ee]{episode:02d}(?!\d)"),
            re.compile(rf"[Ee]{episode:02d}(?!\d)"),
            re.compile(rf"[Ee]{episode}(?!\d)"),
            re.compile(rf"\b{season}\.{episode:02d}\b"),
            re.compile(rf"\b{season}\.{episode}\b"),
        ]
        for name, is_dir in entries:
            if is_dir or not name.lower().endswith(".mp3"):
                continue
            stem = Path(name).stem
            for pat in patterns:
                if pat.search(stem):
                    logger.info("LivingAudio: matched episode via pattern → %s", name)
                    return f"{remote_dir}/{name}"
        return None

    def _match_series(self, tv_folder: str, series: str) -> Optional[str]:
        """Return the best-matching series directory name, or None."""
        entries = self._listdir(tv_folder)
        series_lower = series.lower()

        best_score = 0.0
        best_name: Optional[str] = None
        for name, is_dir in entries:
            if not is_dir:
                continue
            score = _title_similarity(series_lower, name.lower())
            if score > best_score:
                best_score = score
                best_name = name

        if best_score < 0.3 or best_name is None:
            logger.warning(
                "LivingAudio: no series match for %r (best score %.2f)", series, best_score
            )
            return None

        logger.info(
            "LivingAudio: matched %r → %r (score %.2f)", series, best_name, best_score
        )
        return best_name

    def _download(self, remote_path: str, local_path: Path) -> Optional[Path]:
        ftp = self._connect()
        logger.info("LivingAudio: downloading %s", remote_path)
        try:
            with open(local_path, "wb") as f:
                ftp.retrbinary(f"RETR {remote_path}", f.write)
            return local_path
        except ftplib.error_perm as exc:
            logger.warning("LivingAudio: could not retrieve %s — %s", remote_path, exc)
            local_path.unlink(missing_ok=True)
            return None
