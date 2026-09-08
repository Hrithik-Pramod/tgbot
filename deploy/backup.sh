#!/usr/bin/env bash
#
# Nightly database backup.
#
# Install as a cron job:
#   0 2 * * *  /opt/settlement-bot/deploy/backup.sh >> /var/log/settlement-backup.log 2>&1
#
# Restore:
#   gunzip -c settlement-2026-09-07.sql.gz | psql settlement

set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/var/backups/settlement}"
RETENTION_DAYS="${RETENTION_DAYS:-30}"
DB_NAME="${DB_NAME:-settlement}"

# Optional offsite copy. Set S3_TARGET to something like s3://my-bucket/settlement
# and configure the aws CLI. A backup that only exists on the same server as the
# database is not a backup.
S3_TARGET="${S3_TARGET:-}"

mkdir -p "$BACKUP_DIR"
STAMP="$(date +%Y-%m-%d)"
FILE="$BACKUP_DIR/settlement-$STAMP.sql.gz"

echo "[$(date -Is)] dumping $DB_NAME -> $FILE"
pg_dump "$DB_NAME" | gzip > "$FILE"

# Verify the dump is readable before trusting it. A silently truncated backup is
# worse than no backup, because it stops you looking for another copy.
if ! gzip -t "$FILE"; then
    echo "[$(date -Is)] ERROR: dump failed integrity check" >&2
    exit 1
fi

SIZE=$(du -h "$FILE" | cut -f1)
echo "[$(date -Is)] wrote $FILE ($SIZE)"

if [[ -n "$S3_TARGET" ]]; then
    echo "[$(date -Is)] copying offsite to $S3_TARGET"
    aws s3 cp "$FILE" "$S3_TARGET/"
fi

echo "[$(date -Is)] pruning backups older than $RETENTION_DAYS days"
find "$BACKUP_DIR" -name 'settlement-*.sql.gz' -mtime "+$RETENTION_DAYS" -delete

echo "[$(date -Is)] done"
