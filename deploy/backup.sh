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

# This is the BARE-METAL script: it expects PostgreSQL and pg_dump on the host.
# The live deployment runs the database in a container, where this cannot work,
# and reaching for the obviously-named script at the moment you need a backup
# is exactly when a confusing failure costs the most. 18 September 2026: run
# before a destructive operation, and it failed with "pg_dump: command not
# found" — harmless, but the wrong answer to a question asked in a hurry.
if [[ ! -x "$(command -v pg_dump || true)" ]] && [[ -f "$(dirname "$0")/../docker-compose.yml" ]]; then
    echo "This deployment runs PostgreSQL in a container, so pg_dump is not on" >&2
    echo "the host. Use the Docker backup instead:" >&2
    echo >&2
    echo "    cd $(cd "$(dirname "$0")/.." && pwd) && ./deploy/docker-backup.sh" >&2
    exit 2
fi

mkdir -p "$BACKUP_DIR"
STAMP="$(date +%Y-%m-%d)"
FILE="$BACKUP_DIR/settlement-$STAMP.sql.gz"

echo "[$(date -Is)] dumping $DB_NAME -> $FILE"
pg_dump "$DB_NAME" | gzip > "$FILE"

# Verify the dump is readable before trusting it. A silently truncated backup is
# worse than no backup, because it stops you looking for another copy.
if ! gzip -t "$FILE"; then
    echo "[$(date -Is)] ERROR: dump failed integrity check" >&2
    rm -f "$FILE"
    exit 1
fi

# An empty dump still gzips cleanly and still passes the check above, so it
# would be reported as a good backup. docker-backup.sh has always had this
# guard; this one did not.
if [[ $(stat -c%s "$FILE") -lt 1024 ]]; then
    echo "[$(date -Is)] ERROR: dump is suspiciously small — check the database" >&2
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
