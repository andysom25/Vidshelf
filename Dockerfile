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
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create download directories
RUN mkdir -p downloads downloads/music_videos

EXPOSE 5000

CMD ["python", "app.py"]