# ad-sync

Automatically syncs audio descriptions from [AudioVault](https://audiovault.net) with TV episodes and movies downloaded by [Sonarr](https://sonarr.tv) and [Radarr](https://radarr.video).

When Sonarr or Radarr imports a new file, ad-sync:

1. Searches AudioVault for a matching audio description track.
2. Downloads the file (caching season ZIPs so they are only fetched once per season).
3. Runs [describealign](https://github.com/julbean/describealign) to align and combine the audio description with the video.
4. If the alignment score is **65 % or above**, replaces the original file in-place with the combined version. Otherwise the audio description is discarded and the original is untouched.

---

## Requirements

- An AudioVault account (<https://audiovault.net/register>)
- Sonarr v3+ and/or Radarr v3+
- Docker (recommended) **or** Python 3.10+ with `ffmpeg` installed

---

## Setup — Docker (recommended)

This is the recommended approach if Sonarr/Radarr run in Docker, since the Custom Script executes inside each container and needs access to the same Python environment.

### 1. Create your `.env` file

```env
AUDIOVAULT_EMAIL=your@email.com
AUDIOVAULT_PASSWORD=yourpassword

# Minimum alignment score to keep a combined file (default: 65)
AD_SYNC_MIN_SCORE=65
```

Keep this file private (`chmod 600 .env`).

### 2. Start the container

Copy `compose.example.yaml` to `compose.yaml`, adjust the volume paths to match what your Sonarr/Radarr containers mount (e.g. `/tv`, `/movies`), and set the network name to the one your arr containers share:

```bash
cp compose.example.yaml compose.yaml
# edit compose.yaml
docker compose up -d
```

The container starts a small webhook server on port 8686 (internal only — no need to expose it).

### 3. Add the hook script to Sonarr and Radarr

Sonarr/Radarr execute Custom Scripts inside their own containers. The easiest place to drop a script that's already visible inside each container is their `config` volume (mounted at `/config`).

Create `/path/to/sonarr/config/ad-sync-hook.sh`:

```sh
#!/bin/sh
curl -sf -X POST http://ad-sync:8686/hook \
  --data-urlencode "sonarr_eventtype=$sonarr_eventtype" \
  --data-urlencode "sonarr_series_title=$sonarr_series_title" \
  --data-urlencode "sonarr_episodefile_seasonnumber=$sonarr_episodefile_seasonnumber" \
  --data-urlencode "sonarr_episodefile_episodenumbers=$sonarr_episodefile_episodenumbers" \
  --data-urlencode "sonarr_episodefile_path=$sonarr_episodefile_path"
```

Create `/path/to/radarr/config/ad-sync-hook.sh`:

```sh
#!/bin/sh
curl -sf -X POST http://ad-sync:8686/hook \
  --data-urlencode "radarr_eventtype=$radarr_eventtype" \
  --data-urlencode "radarr_movie_title=$radarr_movie_title" \
  --data-urlencode "radarr_movie_year=$radarr_movie_year" \
  --data-urlencode "radarr_moviefile_path=$radarr_moviefile_path"
```

Make both executable:

```bash
chmod +x /path/to/sonarr/config/ad-sync-hook.sh
chmod +x /path/to/radarr/config/ad-sync-hook.sh
```

### 4. Configure Sonarr and Radarr

In each app: **Settings → Connect → + → Custom Script**

| Field | Value |
|---|---|
| Name | `ad-sync` |
| On Import | ✅ |
| On Upgrade | ✅ |
| Path | `/config/ad-sync-hook.sh` |

Click **Test** — you should see a green tick. Then **Save**.

---

## Setup — bare metal / non-Docker

If Sonarr and Radarr run directly on the host (not in Docker), you can install ad-sync as a regular Python package and point the Custom Script at the binary.

### Install

```bash
pip install git+https://github.com/matalvernaz/ad-sync.git
```

`describealign` is a dependency but its GUI component (wxPython) is not needed and may fail to build on some platforms. If the install fails due to wxPython, install without it:

```bash
pip install --no-deps describealign
pip install "ffmpeg-python~=0.2.0" "static-ffmpeg~=3.0" "matplotlib~=3.9" \
    "numpy<3.0,>=1.21" "scipy~=1.10" "platformdirs~=4.2" \
    "natsort~=8.4.0" "sortedcontainers~=2.4.0" future
pip install --no-deps git+https://github.com/matalvernaz/ad-sync.git
```

### Configure

```bash
mkdir -p ~/.config/ad-sync
cp .env.example ~/.config/ad-sync/.env
nano ~/.config/ad-sync/.env
```

### Verify credentials

```bash
ad-sync --test-auth
```

### Set up in Sonarr / Radarr

**Settings → Connect → + → Custom Script**

| Field | Value |
|---|---|
| Name | `ad-sync` |
| On Import | ✅ |
| On Upgrade | ✅ |
| Path | output of `which ad-sync` (e.g. `/usr/local/bin/ad-sync`) |

---

## How it works

### TV episodes (Sonarr)

AudioVault distributes audio descriptions for TV shows as ZIP files containing one MP3 per episode. ad-sync:

1. Searches AudioVault for the series name and season number.
2. Downloads the season ZIP (cached — only downloaded once per season).
3. Extracts the ZIP and finds the right episode MP3 by episode number.
4. Runs describealign on the video + MP3.
5. If the score is ≥ threshold, replaces the original file in-place with the combined version.

### Movies (Radarr)

AudioVault distributes movie audio descriptions as individual MP3 files. ad-sync searches by title, downloads the file, and runs the same alignment + scoring step.

### Caching

Downloaded season ZIPs are stored in `~/.cache/ad-sync/shows/<series>/` (or the Docker container's cache) and are never re-downloaded for the same season.

---

## Manual retry

If AudioVault didn't have an audio description when a file was imported, use the `/retry` endpoint on the running server. All retry requests return **202 Accepted immediately** — processing happens in the background and progress is visible in the container logs.

All paths must be paths *inside the ad-sync container*, which are the same as what Sonarr/Radarr see (i.e. the same volume mounts).

### Single TV episode

```
http://localhost:8686/retry?title=Ted&season=1&episode=3&path=/tv/ted/Season%201/ted.S01E03.mkv
```

### Whole season

Provide `dir=` pointing to the season directory. ad-sync scans for video files and parses `SxxExx` from each filename automatically.

```
http://localhost:8686/retry?title=Ted&season=1&dir=/tv/ted/Season%201
```

The `season=` parameter is optional here — it's used only to filter files if the directory contains episodes from multiple seasons.

### Whole show

Omit `season=` and point `dir=` at the show root. All seasons are queued and processed in order.

```
http://localhost:8686/retry?title=Ted&dir=/tv/ted
```

### Movie

```
http://localhost:8686/retry?title=Inception&year=2010&path=/movies/Inception/Inception.2010.mkv
```

`year=` is optional but improves matching when multiple results share the same title.

---

> **Accessing the endpoint:** If ad-sync is not exposed on port 8686 externally, trigger retries from another container on the same Docker network:
> ```
> curl "http://ad-sync:8686/retry?title=Ted&season=2&dir=/tv/ted/Season%202"
> ```
> Or open a shell into the ad-sync container and use curl from there.

---

## Troubleshooting

**"Login failed"** — Double-check your AudioVault credentials and run `ad-sync --test-auth`.

**"No AudioVault results"** — The show or movie may not have an audio description on AudioVault yet. Use the [manual retry](#manual-retry) endpoint once it becomes available.

**"Score X% is below threshold"** — The audio description didn't align well with the video (possibly a different version/cut). The original file is untouched.

**Green tick on Test but nothing happens on import** — Check that the hook script is executable and that the `ad-sync` container is on the same Docker network as Sonarr/Radarr.

---

## Adjusting the score threshold

Set `AD_SYNC_MIN_SCORE=50` in your `.env` to accept lower-quality alignments. The describealign documentation notes that scores below 20 % are likely mismatched files and scores above 90 % may indicate undescribed media.

---

## License

MIT
