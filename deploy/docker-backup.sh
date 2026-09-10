#!/usr/bin/env bash
#
# Nightly database backup for the Docker deployment.
#
# Install as a cron job (adjust the path to wherever you cloned the repo):
#   0 2 * * *  cd /opt/settlement-bot && ./deploy/docker-backup.sh >> /var/log/settlement-backup.log 2>&1
#
# Restore:
#   gunzip -c settlement-2026-09-08.sql.gz | docker compose exec -T db psql -U settlement -d settlement

set -euo pipefail

cd "$(dirname "$0")/.."

BACKUP_DIR="${BACKUP_DIR:-/var/backups/settlement}"
RETENTION_DAYS="${RETENTION_DAYS:-30}"
DB_USER="${POSTGRES_USER:-settlement}"
DB_NAME="${POSTGRES_DB:-settlement}"

# Optional offsite copy. A backup that lives only on the same server as the
# database is not a backup.
S3_TARGET="${S3_TARGET:-}"

mkdir -p "$BACKUP_DIR"
STAMP="$(date +%Y-%m-%d)"
FILE="$BACKUP_DIR/settlement-$STAMP.sql.gz"

echo "[$(date -Is)] dumping $DB_NAME -> $FILE"

# -T because cron has no TTY. Without it docker refuses to run.
docker compose exec -T db pg_dump -U "$DB_USER" "$DB_NAME" | gzip > "$FILE"

# Verify before trusting it. A silently truncated backup is worse than none,
# because it stops you looking for another copy.
if ! gzip -t "$FILE"; then
    echo "[$(date -Is)] ERROR: dump failed integrity check" >&2
    rm -f "$FILE"
    exit 1
fi

# A dump of an empty database still gzips cleanly, so check it has real content.
if [[ $(stat -c%s "$FILE") -lt 1024 ]]; then
    echo "[$(date -Is)] ERROR: dump is suspiciously small — check the database" >&2
    exit 1
fi

echo "[$(date -Is)] wrote $FILE ($(du -h "$FILE" | cut -f1))"

if [[ -n "$S3_TARGET" ]]; then
    echo "[$(date -Is)] copying offsite to $S3_TARGET"
    aws s3 cp "$FILE" "$S3_TARGET/"
fi

echo "[$(date -Is)] pruning backups older than $RETENTION_DAYS days"
find "$BACKUP_DIR" -name 'settlement-*.sql.gz' -mtime "+$RETENTION_DAYS" -delete

echo "[$(date -Is)] done"
