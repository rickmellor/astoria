# Astoria memory service — runs on the always-on NAS (UGREEN DXP4800GT, x86_64).
# Pattern mirrors memoryos-service / megaplan-service: python-slim, non-root UID 1000,
# single uvicorn process that ALSO hosts the background workers (cognify queue drain,
# curator, backups) — UGOS blocks user cron, so scheduling lives in-container.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

# pg_dump for in-container backups (matches the pg18 server major).
RUN apt-get update && apt-get install -y --no-install-recommends curl gnupg ca-certificates \
 && install -d /usr/share/postgresql-common/pgdg \
 && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
 && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" > /etc/apt/sources.list.d/pgdg.list \
 && apt-get update && apt-get install -y --no-install-recommends postgresql-client-18 \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY astoria ./astoria
RUN pip install . \
 && useradd -u 1000 -m astoria && mkdir -p /data /backups && chown -R astoria:astoria /data /backups
USER astoria

EXPOSE 8933
HEALTHCHECK --interval=60s --timeout=15s --start-period=40s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8933/health', timeout=10)"

CMD ["uvicorn", "astoria.api.app:app", "--host", "0.0.0.0", "--port", "8933"]
