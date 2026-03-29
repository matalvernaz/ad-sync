"""
Fuzzy matching helpers.

Matches search results from AudioVault against show/movie titles and
locates the correct episode MP3 inside an extracted season zip.
"""

from __future__ import annotations

import logging
import re
import zipfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Audio file extensions that describealign accepts.
_AUDIO_EXTS = {".mp3", ".m4a", ".opus", ".wav", ".aac", ".flac", ".ac3", ".mka"}


# ------------------------------------------------------------------
# Title / season matching
# ------------------------------------------------------------------

def find_season(results: list[dict], title: str, season: int) -> list[dict]:
    """
    Return all results from *results* that plausibly match *title* and *season*,
    ranked by title similarity (best first).

    Pass 1 returns candidates that explicitly name the season (e.g. "Season 2").
    Pass 2 (season 1 only) appends year-only entries (e.g. "Ted (2024)") as
    lower-priority fallbacks, for shows AudioVault hasn't split into seasons yet.

    The caller should try each candidate in order, stopping on the first that
    aligns above the score threshold.
    """
    season_tokens = {
        f"season {season:02d}",
        f"season {season}",
        f"s{season:02d}",
        f"series {season:02d}",
        f"series {season}",
    }

    title_lower = title.lower()

    def _ranked_above(candidates: list[dict], threshold: float) -> list[dict]:
        scored = [(_title_similarity(title_lower, r["name"].lower()), r) for r in candidates]
        scored.sort(key=lambda x: x[0], reverse=True)
        kept = [(s, r) for s, r in scored if s >= threshold]
        for s, r in kept:
            logger.info("Season candidate: %r (score %.2f)", r["name"], s)
        if scored and not kept:
            logger.warning(
                "Best season match %r has low similarity (%.2f) — skipping.",
                scored[0][1]["name"], scored[0][0],
            )
        return [r for _, r in kept]

    # Pass 1: results that explicitly name the season.
    with_token = [r for r in results if any(tok in r["name"].lower() for tok in season_tokens)]
    candidates = _ranked_above(with_token, 0.3)

    # Pass 2 (season 1 only): year-only entries like "Ted (2024)" that
    # AudioVault uses for shows not yet split into numbered seasons.
    if season == 1:
        all_season_tokens = {
            tok
            for n in range(1, 20)
            for tok in (f"season {n}", f"series {n}", f"s{n:02d}")
        }
        without_token = [
            r for r in results
            if not any(tok in r["name"].lower() for tok in all_season_tokens)
        ]
        pass2 = _ranked_above(without_token, 0.4)
        if pass2:
            logger.info("Season 1: also queued %d year-only fallback(s).", len(pass2))
        candidates = candidates + pass2

    if not candidates:
        logger.warning("No season %d candidates found for %r.", season, title)

    return candidates


def find_movie(results: list[dict], title: str, year: str) -> list[dict]:
    """
    Return all results from *results* that plausibly match *title* (and
    optionally *year*), ranked by score (best first).

    The caller should try each candidate in order, stopping on the first that
    aligns above the score threshold.
    """
    title_lower = title.lower()
    scored: list[tuple[float, dict]] = []

    for result in results:
        name_lower = result["name"].lower()
        score = _title_similarity(title_lower, name_lower)

        if year and year in result["name"]:
            score += 0.15  # small bonus for year match

        scored.append((score, result))

    scored.sort(key=lambda x: x[0], reverse=True)
    kept = [(s, r) for s, r in scored if s >= 0.3]

    for s, r in kept:
        logger.info("Movie candidate: %r (score %.2f)", r["name"], s)

    if scored and not kept:
        logger.warning(
            "Best movie match %r has low similarity (%.2f) — skipping.",
            scored[0][1]["name"], scored[0][0],
        )

    return [r for _, r in kept]


# ------------------------------------------------------------------
# Episode extraction
# ------------------------------------------------------------------

def extract_episode(zip_path: Path, extract_dir: Path, episode: int) -> Optional[Path]:
    """
    Extract *zip_path* into *extract_dir* (if not already done) and return
    the audio file for *episode*.

    Episode matching tries several patterns in order:
      1. Explicit SxxEnn or Exx pattern in the filename.
      2. epNN or episodeNN pattern.
      3. Positional fallback (nth audio file sorted lexicographically).
    """
    # If the "file" is actually already an MP3/audio, return it directly.
    if zip_path.suffix.lower() in _AUDIO_EXTS:
        return zip_path

    _ensure_extracted(zip_path, extract_dir)

    audio_files = sorted(
        f for f in extract_dir.rglob("*") if f.is_file() and f.suffix.lower() in _AUDIO_EXTS
    )

    if not audio_files:
        logger.error("No audio files found after extracting %s.", zip_path.name)
        return None

    # Pattern list, tried in order.
    patterns = [
        re.compile(rf"[Ee]{episode:02d}(?!\d)"),
        re.compile(rf"[Ee]{episode}(?!\d)"),
        re.compile(rf"[Ee]p(?:isode)?\.?\s*0*{episode}(?!\d)", re.IGNORECASE),
    ]

    for audio in audio_files:
        for pattern in patterns:
            if pattern.search(audio.stem):
                logger.info("Matched episode %02d → %s", episode, audio.name)
                return audio

    # Positional fallback (1-based). Episode 0 is excluded: it means "special
    # episode", and positional index -1 would be meaningless; the filename
    # patterns above must match explicitly for specials.
    if episode == 0:
        logger.error(
            "Episode 00 (special) not found — the zip filename must contain E00 or similar."
        )
        return None

    if 1 <= episode <= len(audio_files):
        chosen = audio_files[episode - 1]
        logger.warning(
            "No filename match for E%02d; using positional fallback → %s",
            episode,
            chosen.name,
        )
        return chosen

    logger.error("Episode %02d not found among %d audio files.", episode, len(audio_files))
    return None


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _ensure_extracted(zip_path: Path, extract_dir: Path) -> None:
    """Extract *zip_path* into *extract_dir* only if not already done."""
    extract_dir.mkdir(parents=True, exist_ok=True)

    marker = extract_dir / ".extracted"
    if marker.exists():
        return

    logger.info("Extracting %s → %s", zip_path.name, extract_dir)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extract_dir)

    marker.touch()


def _title_similarity(a: str, b: str) -> float:
    """Jaccard similarity on word tokens, ignoring common noise words."""
    _STOPWORDS = {"the", "a", "an", "and", "of", "in", "to", "for", "season", "series"}

    def tokenize(s: str) -> set[str]:
        s = re.sub(r"[^\w\s]", " ", s.lower())
        return set(s.split()) - _STOPWORDS

    tokens_a = tokenize(a)
    tokens_b = tokenize(b)

    if not tokens_a or not tokens_b:
        return 0.0

    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)
