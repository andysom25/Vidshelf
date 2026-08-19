FROM python:3.12-slim

# Unbuffered stdout so log lines from concurrent download threads print in
# real chronological order instead of getting held in Python's buffer and
# flushed out of order (this made working downloads look broken in `docker
# logs` - see REFERENCE.md).
ENV PYTHONUNBUFFERED=1

# ffmpeg for yt-dlp format merging; fonts-dejavu-core for rendering the
# per-video title-card posters (artwork_sync.py's generate_title_card()) —
# without it, Pillow silently falls back to its tiny bitmap default font.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fonts-dejavu-core \
    curl \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# Deno — yt-dlp's YouTube extractor needs an external JS runtime to solve
# YouTube's signature/n-parameter/PoToken challenge (its "EJS" requirement;
# see https://github.com/yt-dlp/yt-dlp/wiki/EJS). Without one, extraction logs
# "No supported JavaScript runtime could be found" and falls back to formats
# whose signed googlevideo.com URLs are invalid or throttled — downloads start
# fine and then die mid-stream with HTTP 403, every single time, once YouTube
# stops accepting the un-solved fallback (see REFERENCE.md, 2026-08-19). Deno
# is what yt-dlp auto-detects with zero extra config, so no --js-runtimes flag
# is needed. curl/unzip above are only for this install step, not runtime deps
# of the app itself.
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh \
    && apt-get purge -y --auto-remove curl unzip \
    && rm -rf /var/lib/apt/lists/* /root/.cache

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create download directories, plus the state directory. /app/data is
# normally a mounted volume; creating it here means an unmounted run (plain
# `docker run` with no -v) still starts cleanly instead of failing on the
# first config write.
RUN mkdir -p downloads downloads/music_videos data

EXPOSE 5000

# Without this, `restart: unless-stopped` can't tell a wedged app from a healthy
# one — the container stays "up" while serving nothing. Hits /login because it's
# the only route that answers without a session. Uses urllib rather than curl so
# the slim image doesn't need an extra package.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5000/login', timeout=4).status == 200 else 1)" || exit 1

CMD ["python", "app.py"]