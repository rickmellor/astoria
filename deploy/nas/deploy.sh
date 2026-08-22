#!/usr/bin/env bash
# Deploy Astoria to the NAS: tar-over-ssh (UGOS rsync is restricted), then compose up --build.
#   ./deploy/nas/deploy.sh            # build + (re)start
#   ./deploy/nas/deploy.sh --no-build # restart only
# Requires: ssh alias `root-dxp4800gt`; /volume1/docker/astoria/.env already present (secrets, never in git).
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
NAS="${ASTORIA_NAS_SSH:-root-dxp4800gt}"
DEST="${ASTORIA_NAS_DIR:-/volume1/docker/astoria}"
BUILD="--build"; [[ "${1:-}" == "--no-build" ]] && BUILD=""

echo "→ syncing $REPO → $NAS:$DEST/src"
ssh "$NAS" "mkdir -p $DEST/src $DEST/pgdata $DEST/backups $DEST/data && chown 1000:1000 $DEST/data $DEST/backups 2>/dev/null || true"
tar -C "$REPO" --exclude=.git --exclude=.venv --exclude='__pycache__' --exclude='*.pyc' \
    --exclude='deploy/nas/.env' --exclude='deploy/nas/pgdata' --exclude='deploy/nas/backups' --exclude='deploy/nas/data' \
    -czf - . | ssh "$NAS" "tar -xzf - -C $DEST/src"
# compose + .env live at $DEST (stable across redeploys); the compose build context is ../../ == src/
ssh "$NAS" "cp $DEST/src/deploy/nas/docker-compose.yml $DEST/src/deploy/nas/docker-compose.yml.bak 2>/dev/null || true; \
            sed 's#context: ../..#context: ./src#' $DEST/src/deploy/nas/docker-compose.yml > $DEST/docker-compose.yml; \
            test -f $DEST/.env || { echo '!! $DEST/.env missing — copy deploy/nas/.env.example and fill secrets'; exit 2; }; \
            cd $DEST && docker compose up -d $BUILD --remove-orphans && docker compose ps"
echo "→ health:"; sleep 3
curl -s --max-time 10 "http://192.168.1.134:8933/health" | head -c 600; echo
