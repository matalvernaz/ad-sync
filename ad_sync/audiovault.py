"""AudioVault client: login, search, and download audio description files."""

from __future__ import annotations

import json
import logging
import re
from datetime import date
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BASE_URL = "https://audiovault.net"

# Mimic a real Firefox request so the server doesn't reject us outright.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) "
        "Gecko/20100101 Firefox/124.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-CA,en;q=0.5",
}


class LoginError(RuntimeError):
    """Raised when AudioVault login fails."""


class DailyLimitReached(RuntimeError):
    """Raised when the AudioVault 25-downloads-per-day limit would be exceeded."""


class DownloadLimiter:
    """
    Tracks AudioVault downloads against the 25-per-day limit.

    State is persisted to *state_path* as a small JSON file so the count
    survives process restarts.  The date resets automatically at midnight.
    """

    DAILY_LIMIT = 25

    def __init__(self, state_path: Path) -> None:
        self._path = state_path

    def check_and_increment(self) -> None:
        """Increment the counter or raise DailyLimitReached."""
        today = date.today().isoformat()
        state = self._load()
        if state.get("date") != today:
            state = {"date": today, "count": 0}
        if state["count"] >= self.DAILY_LIMIT:
            raise DailyLimitReached(
                f"AudioVault daily download limit ({self.DAILY_LIMIT}) reached. "
                "The counter resets at midnight."
            )
        state["count"] += 1
        self._save(state)
        logger.info(
            "AudioVault downloads today: %d/%d", state["count"], self.DAILY_LIMIT
        )

    def _load(self) -> dict:
        if self._path.exists():
            try:
                return json.loads(self._path.read_text())
            except (json.JSONDecodeError, ValueError):
                pass
        return {}

    def _save(self, state: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(state))


class AudioVaultClient:
    """Authenticated session for AudioVault."""

    def __init__(self, email: str, password: str) -> None:
        self._email = email
        self._password = password
        self._session = requests.Session()
        self._session.headers.update(_HEADERS)
        self._login()

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _login(self) -> None:
        # Fetch the login page to collect the Laravel CSRF token.
        resp = self._session.get(f"{BASE_URL}/login", timeout=30)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")
        token_input = soup.find("input", {"name": "_token"})
        if not token_input:
            raise LoginError("Could not find CSRF token on login page.")

        payload = {
            "_token": token_input["value"],
            "email": self._email,
            "password": self._password,
            "remember": "on",
        }

        resp = self._session.post(
            f"{BASE_URL}/login",
            data=payload,
            timeout=30,
            allow_redirects=True,
        )
        resp.raise_for_status()

        # A successful login redirects away from /login.
        if resp.url.rstrip("/").endswith("/login"):
            raise LoginError(
                "Login failed — check your AUDIOVAULT_EMAIL and AUDIOVAULT_PASSWORD."
            )

        logger.info("Logged in to AudioVault successfully.")

    def _relogin(self) -> None:
        """Re-create the session and log in again after a session expiry."""
        logger.info("Session expired — re-logging in to AudioVault.")
        self._session = requests.Session()
        self._session.headers.update(_HEADERS)
        self._login()

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search_shows(self, title: str) -> list[dict]:
        """Return a list of {'name': ..., 'url': ...} for matching TV seasons."""
        return self._search("/shows", title)

    def search_movies(self, title: str) -> list[dict]:
        """Return a list of {'name': ..., 'url': ...} for matching movies."""
        return self._search("/movies", title)

    def _search(self, path: str, query: str) -> list[dict]:
        resp = self._session.get(
            f"{BASE_URL}{path}",
            params={"search": query},
            timeout=30,
        )
        resp.raise_for_status()
        if resp.url.rstrip("/").endswith("/login"):
            self._relogin()
            resp = self._session.get(
                f"{BASE_URL}{path}",
                params={"search": query},
                timeout=30,
            )
            resp.raise_for_status()
        return _parse_results_table(resp.text)

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

    def download(self, url: str, dest_dir: Path) -> Path:
        """
        Download *url* into *dest_dir* and return the saved file path.

        The filename is taken from the Content-Disposition header when
        present, falling back to the last URL path segment.
        """
        dest_dir.mkdir(parents=True, exist_ok=True)

        with self._session.get(url, stream=True, timeout=120) as resp:
            resp.raise_for_status()

            content_disp = resp.headers.get("Content-Disposition", "")
            match = re.search(r'filename\*?=["\']?(?:UTF-8\'\')?([^"\';\r\n]+)', content_disp)
            if match:
                filename = match.group(1).strip()
            else:
                filename = url.rstrip("/").split("/")[-1]

            # Sanitise the filename so it is safe for all filesystems.
            filename = re.sub(r'[\\/:*?"<>|]', "_", filename)
            dest = dest_dir / filename

            with dest.open("wb") as fh:
                for chunk in resp.iter_content(chunk_size=65_536):
                    fh.write(chunk)

        logger.info("Downloaded: %s", dest)
        return dest


# ------------------------------------------------------------------
# HTML parsing helpers
# ------------------------------------------------------------------

def _parse_results_table(html: str) -> list[dict]:
    """
    Parse the search-results table (ID | Name | Download) from a shows/movies page.
    Returns a list of dicts with 'name' and 'url' keys.
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if not table:
        return []

    results: list[dict] = []
    for row in table.find_all("tr")[1:]:  # skip the header row
        cells = row.find_all("td")
        if len(cells) < 3:
            continue

        name = cells[1].get_text(strip=True)
        link = cells[2].find("a", href=True)
        if not link:
            continue

        href: str = link["href"]
        if not href.startswith("http"):
            href = BASE_URL + href

        results.append({"name": name, "url": href})

    return results
