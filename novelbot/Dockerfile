# ---------------------------------------------------------------------
#  Optional Docker path (works on Render's Docker runtime too).
#  Slim base + no build toolchain => small image, fast cold start.
# ---------------------------------------------------------------------
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/tmp/novelbot

WORKDIR /app

# No OS packages are required: TgCrypto ships manylinux wheels, and the bot
# writes only to /tmp.
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY app ./app
COPY resources ./resources
COPY main.py ./
COPY sql ./sql

# Render injects $PORT; 8000 is the local default.
EXPOSE 8000

# Non-root for hygiene; /tmp is world-writable so the session file still works.
RUN useradd -m -u 10001 botuser && chown -R botuser:botuser /app
USER botuser

HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD python -c "import os,urllib.request;urllib.request.urlopen(f\"http://127.0.0.1:{os.getenv('PORT','8000')}/health\",timeout=4)"

CMD ["python", "main.py"]
