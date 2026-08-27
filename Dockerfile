# Astoria memory service — runs on the always-on NAS (UGREEN DXP4800GT, x86_64).
# Pattern mirrors memoryos-service / megaplan-service: python-slim, non-root UID 1000,
# single uvicorn process that ALSO hosts the background workers (cognify queue drain,
# curator) — UGOS blocks user cron, so scheduling lives in-container. Backups are a
# separate pg_dump sidecar (see deploy/nas/docker-compose.yml).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY pyproject.toml README.md ./
COPY astoria ./astoria
RUN pip install . \
 && useradd -u 1000 -m astoria && mkdir -p /data && chown -R astoria:astoria /data
USER astoria

EXPOSE 8933
HEALTHCHECK --interval=60s --timeout=15s --start-period=40s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8933/health', timeout=10)"

CMD ["uvicorn", "astoria.api.app:app", "--host", "0.0.0.0", "--port", "8933", "--workers", "1"]
