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

def find_season(results: list[dict], title: str, season: int) -> Optional[dict]:
    """
    Return the best result from *results* that matches *title* and *season*.

    Results are scored by title overlap; season must appear literally.
    For season 1, also considers year-only entries (e.g. "Ted (2024)") that
    AudioVault sometimes uses instead of "Ted - Season 1 (2024)".
    """
    season_tokens = {
        f"season {season:02d}",
        f"season {season}",
        f"s{season:02d}",
        f"series {season:02d}",
        f"series {season}",
    }

    title_lower = title.lower()

    def _best_above_threshold(candidates: list[dict], threshold: float) -> Optional[dict]:
        scored = [(_title_similarity(title_lower, r["name"].lower()), r) for r in candidates]
        scored.sort(key=lambda x: x[0], reverse=True)
        if not scored:
            return None
        best_score, best = scored[0]
        if best_score < threshold:
            logger.warning(
                "Best season match %r has low similarity (%.2f) — skipping.",
                best["name"], best_score,
            )
            return None
        logger.info("Best season match: %r (score %.2f)", best["name"], best_score)
        return best

    # Pass 1: results that explicitly name the season.
    with_token = [r for r in results if any(tok in r["name"].lower() for tok in season_tokens)]
    result = _best_above_threshold(with_token, 0.3)
    if result:
        return result

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
        result = _best_above_threshold(without_token, 0.4)
        if result:
            logger.info("Season 1 matched via year-only entry: %r", result["name"])
            return result

    return None


def find_movie(results: list[dict], title: str, year: str) -> Optional[dict]:
    """
    Return the best result from *results* that matches *title* (and optionally *year*).

    Returns None if the best score is below a minimum threshold.
    """
    title_lower = title.lower()
    scored: list[tuple[float, dict]] = []

    for result in results:
        name_lower = result["name"].lower()
        score = _title_similarity(title_lower, name_lower)

        if year and year in result["name"]:
            score += 0.15  # small bonus for year match

        scored.append((score, result))

    if not scored:
        return None

    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, best = scored[0]

    if best_score < 0.3:
        logger.warning(
            "Best movie match %r has low similarity (%.2f) — skipping.", best["name"], best_score
        )
        return None

    logger.info("Best movie match: %r (score %.2f)", best["name"], best_score)
    return best


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

    # Positional fallback (1-based).
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
