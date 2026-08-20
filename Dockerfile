# copier-bot — read-only MT5 observer. Runs the live shadow-mode pipeline.
# The MT5 connection lives in MetaApi's cloud, so this image is a small Linux
# Python process — no Windows/MetaTrader needed.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install dependencies first (layer cache). The build backend needs pyproject +
# README + the package source.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[live]"

# Runtime files (non-secret config; secrets come from the environment).
COPY config ./config
COPY docker/healthcheck.py ./docker/healthcheck.py

# Persistent SQLite lives here — mount a volume so dedupe/state survive restarts.
VOLUME ["/data"]

# Container-level liveness: fail if the heartbeat in the DB is older than 120s.
# (Complements the external dead-man's switch, which catches total host death.)
HEALTHCHECK --interval=60s --timeout=10s --start-period=90s --retries=3 \
    CMD python docker/healthcheck.py /data/copier.db 120 || exit 1

CMD ["python", "-m", "copier.main", "--live", "--db", "/data/copier.db"]
