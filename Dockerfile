FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py downloader.py messages.py config.py recognizer.py ./

RUN useradd -m -u 1000 botuser \
    && mkdir -p /app/temp \
    && touch /app/user_langs.json /app/cookies.txt \
    && chown -R botuser:botuser /app

USER botuser

CMD ["python", "-u", "bot.py"]
