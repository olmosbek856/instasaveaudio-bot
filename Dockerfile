FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        coreutils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py downloader.py messages.py config.py recognizer.py db.py ./

RUN useradd -m -u 1000 botuser \
    && mkdir -p /app/temp /app/data \
    && touch /app/cookies.txt \
    && chown -R botuser:botuser /app

USER botuser

# Healthcheck: bot.py touches /app/data/health every 30s. Stale (>120s) ⇒ unhealthy.
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
    CMD test -f /app/data/health \
        && test $(( $(date +%s) - $(stat -c %Y /app/data/health) )) -lt 120 \
        || exit 1

CMD ["python", "-u", "bot.py"]
