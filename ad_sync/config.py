"""Configuration loaded from a .env file or environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Search for .env in the current directory, then ~/.config/ad-sync/.env
_CONFIG_PATHS = [
    Path.cwd() / ".env",
    Path.home() / ".config" / "ad-sync" / ".env",
]

for _path in _CONFIG_PATHS:
    if _path.exists():
        load_dotenv(_path)
        break


@dataclass
class Config:
    email: str
    password: str
    min_score: float = 65.0
    cache_dir: Path = field(
        default_factory=lambda: Path.home() / ".cache" / "ad-sync"
    )
    stretch_audio: bool = True

    @classmethod
    def from_env(cls) -> "Config":
        email = os.environ.get("AUDIOVAULT_EMAIL", "").strip()
        password = os.environ.get("AUDIOVAULT_PASSWORD", "").strip()

        if not email or not password:
            raise ValueError(
                "AUDIOVAULT_EMAIL and AUDIOVAULT_PASSWORD must be set. "
                "Copy .env.example to ~/.config/ad-sync/.env and fill in your credentials."
            )

        min_score = float(os.environ.get("AD_SYNC_MIN_SCORE", "65"))

        raw_cache = os.environ.get("AD_SYNC_CACHE_DIR", "")
        cache_dir = Path(raw_cache).expanduser() if raw_cache else Path.home() / ".cache" / "ad-sync"

        stretch_audio = os.environ.get("AD_SYNC_STRETCH_AUDIO", "true").strip().lower() != "false"

        return cls(email=email, password=password, min_score=min_score, cache_dir=cache_dir, stretch_audio=stretch_audio)
