#!/bin/sh
# Deploy a new version on the production server (1 GB RAM / 1 CPU, so save resources).
#
# Usage (on the server):
#   cd /opt/mail-gateway && sh scripts/deploy.sh
#
# What it does: Mongo backup -> git pull -> rebuild the image ONLY if
# requirements.txt or src/Dockerfile changed -> up -d -> basic checks.
# The Python code is mounted as a volume (./src:/app/src), so ordinary changes
# take effect by restarting the container without a build.
set -e

cd "$(dirname "$0")/.."
BACKUP_DIR=/root/pre-deploy-backup
STAMP=$(date +%F-%H%M)

echo "==> Free disk space"
df -h / | tail -1
AVAIL_MB=$(df -m / | tail -1 | awk '{print $4}')
if [ "$AVAIL_MB" -lt 700 ]; then
    echo "!! Less than 700 MB free: the build may fail."
    echo "   To clean up: journalctl --vacuum-size=100M; docker system prune -f"
    exit 1
fi

echo "==> Mongo backup to $BACKUP_DIR"
mkdir -p "$BACKUP_DIR"
docker exec mongo mongodump --archive=/tmp/pre-deploy.gz --gzip >/dev/null
docker cp mongo:/tmp/pre-deploy.gz "$BACKUP_DIR/mongo-$STAMP.gz"
docker exec mongo rm -f /tmp/pre-deploy.gz
ls -1t "$BACKUP_DIR"/mongo-*.gz | tail -n +6 | xargs -r rm -f   # keep the 5 latest

echo "==> Local changes (should be empty, otherwise pull will not touch them)"
git status -s || true

BEFORE_REQ=$(md5sum requirements.txt src/Dockerfile 2>/dev/null | md5sum)

echo "==> git pull"
git pull --ff-only

AFTER_REQ=$(md5sum requirements.txt src/Dockerfile 2>/dev/null | md5sum)

if [ "$BEFORE_REQ" != "$AFTER_REQ" ]; then
    echo "==> Dependencies changed: rebuilding the app image"
    docker compose build app
else
    echo "==> requirements.txt / Dockerfile unchanged: no build needed"
fi

echo "==> Starting services"
docker compose up -d

# rspamd configs are mounted into the container but only picked up at start
echo "==> Restarting rspamd (reload local.d)"
docker compose restart rspamd

echo "==> Status"
docker compose ps --format "{{.Name}}\t{{.Status}}"

echo "==> App logs (expecting Database initialized / LMTP / API, no Traceback)"
# The container may not have been recreated: the startup lines are then far from the end of the log
docker logs mail_processor --tail 300 2>&1 \
    | grep -E "Database initialized|LMTP Server started|API server started|Run polling|Traceback|Error" \
    | tail -6 \
    || echo "   (no startup lines in the last 300: the container has been running for a while, this is fine)"

echo "==> Checking that postfix is reachable from app"
docker exec mail_processor python -c "
import socket; s=socket.create_connection(('postfix',25),timeout=8)
print('postfix:25 ->', s.recv(100).decode().strip()); s.close()" || echo "!! postfix is unreachable"

echo "==> Done. Full verification checklist: DEPLOY_CHECKLIST.md"
