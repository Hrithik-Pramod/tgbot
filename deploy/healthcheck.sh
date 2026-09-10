#!/usr/bin/env bash
#
# Full system check. Read-only — changes nothing, safe to run any time.
#
#   bash deploy/healthcheck.sh
#
# Exits 0 if everything passed, 1 if anything FAILED. Warnings do not fail the
# run: they are things worth knowing rather than things that are broken.
#
# Written after the first live day, on which the things that actually went
# wrong were: a setting that did not take effect, a wallet that was never
# adopted, a container running older code than the repo, and backups that had
# never once run. None of those announce themselves — each looks exactly like a
# working system until the moment it matters. So each is checked here.

set -u
cd "$(dirname "$0")/.."

FAILED=0
WARNED=0

if [ -t 1 ]; then G=$'\033[32m'; R=$'\033[31m'; Y=$'\033[33m'; B=$'\033[1m'; N=$'\033[0m'
else G=""; R=""; Y=""; B=""; N=""; fi

ok()   { printf "  ${G}PASS${N}  %s\n" "$1"; }
bad()  { printf "  ${R}FAIL${N}  %s\n" "$1"; FAILED=$((FAILED + 1)); }
warn() { printf "  ${Y}WARN${N}  %s\n" "$1"; WARNED=$((WARNED + 1)); }
head_() { printf "\n${B}%s${N}\n" "$1"; }

if [ -f .env ]; then
    set -a; . ./.env; set +a
else
    echo "No .env found. Run this from the deployment directory."
    exit 1
fi

DBU="${POSTGRES_USER:-settlement}"
DBN="${POSTGRES_DB:-settlement}"

# -qtAX: no headers, no alignment, no psqlrc. One bare value per line.
q() { docker compose exec -T db psql -qtAX -U "$DBU" -d "$DBN" -c "$1" 2>/dev/null | tr -d '\r'; }

# ---------------------------------------------------------------- containers
head_ "Containers"

if docker compose ps --status running 2>/dev/null | grep -q 'bot'; then
    ok "bot container is running"
else
    bad "bot container is NOT running — nothing is being detected"
fi

if [ "$(q 'SELECT 1')" = "1" ]; then
    ok "database is reachable"
else
    bad "database is NOT reachable — every check below is meaningless"
    echo; echo "Stopping here."; exit 1
fi

# ---------------------------------------------------------------- code version
head_ "Code"

LOCAL_REV="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
echo "  repo at $LOCAL_REV"

git fetch -q origin 2>/dev/null
BEHIND="$(git rev-list --count HEAD..origin/main 2>/dev/null || echo 0)"
if [ "${BEHIND:-0}" -gt 0 ]; then
    warn "repo is $BEHIND commit(s) behind origin/main — git pull"
else
    ok "repo is up to date with origin"
fi

# The container runs a built image, not the working tree. A pull without a
# rebuild leaves old code running and looks completely normal.
if docker compose exec -T bot test -f /app/deploy/healthcheck.sh 2>/dev/null; then
    ok "image contains the current deploy tools"
else
    warn "image may predate the latest build — docker compose build bot"
fi

# ---------------------------------------------------------------- settings
head_ "Settings"

check_env() {
    val="$(eval "printf '%s' \"\${$1:-}\"")"
    if [ -z "$val" ]; then
        bad "$1 is not set"
    elif [ -n "${2:-}" ] && [ "$val" != "$2" ]; then
        bad "$1 = $val, expected $2"
    else
        ok "$1 = $val"
    fi
}

check_env MONITOR_ASSET USDT
check_env MIN_DEPOSIT_AMOUNT
check_env NEAR_COMPLETION_INR
check_env POLL_INTERVAL_SECONDS
check_env BRIDGE_CHANNEL_ID
check_env DATABASE_URL

case "$(stat -c%a .env 2>/dev/null)" in
    600|400) ok ".env is not readable by other users" ;;
    *)       warn ".env is mode $(stat -c%a .env 2>/dev/null) — chmod 600 .env" ;;
esac

# ---------------------------------------------------------------- bots
head_ "Bots"

check_bot() {
    body="$(curl -s --max-time 15 "https://api.telegram.org/bot$2/getMe")"
    case "$body" in
        *'"ok":true'*)
            uname="$(printf '%s' "$body" | sed -n 's/.*"username":"\([^"]*\)".*/\1/p')"
            reads="$(printf '%s' "$body" | grep -c '"can_read_all_group_messages":true')"
            if [ "$3" = "yes" ] && [ "$reads" -eq 0 ]; then
                bad "$1 @$uname alive, but privacy mode is ON — it cannot see pasted payments"
            else
                ok "$1 @$uname alive"
            fi
            ;;
        "") bad "$1 — no response from Telegram" ;;
        *)  bad "$1 — $(printf '%s' "$body" | sed -n 's/.*"description":"\([^"]*\)".*/\1/p')" ;;
    esac
}

check_bot "bridge  " "${BRIDGE_BOT_TOKEN:-}"   no
check_bot "supplier" "${SUPPLIER_BOT_TOKEN:-}" yes
check_bot "client  " "${CLIENT_BOT_TOKEN:-}"   yes

# ---------------------------------------------------------------- setup data
head_ "Parties, wallets, rates"

PARTIES="$(q 'SELECT count(*) FROM parties WHERE is_active')"
BRIDGES="$(q "SELECT count(*) FROM parties WHERE role='bridge' AND is_active")"
echo "  $PARTIES active part(ies)"

if [ "${BRIDGES:-0}" -eq 1 ]; then
    ok "exactly one Bridge party"
else
    bad "$BRIDGES bridge parties — authorisation is ambiguous with anything but 1"
fi

BRIDGE_DB="$(q "SELECT telegram_chat_id FROM parties WHERE role='bridge' AND is_active")"
if [ "$BRIDGE_DB" = "${BRIDGE_CHANNEL_ID:-}" ]; then
    ok "BRIDGE_CHANNEL_ID matches the Bridge party's chat"
else
    bad "BRIDGE_CHANNEL_ID=${BRIDGE_CHANNEL_ID:-unset} but the Bridge party is $BRIDGE_DB — alerts go to the wrong place"
fi

UNMON="$(q 'SELECT count(*) FROM wallets WHERE NOT is_monitored')"
if [ "${UNMON:-0}" -eq 0 ]; then
    ok "every wallet is monitored"
else
    bad "$UNMON wallet(s) not monitored — deposits there are invisible"
fi

# An internal wallet with no rate detects deposits but opens no trade.
NORATE="$(q "SELECT count(*) FROM wallets w WHERE w.is_internal AND NOT EXISTS (
              SELECT 1 FROM rates r WHERE r.supplier_id = w.supplier_id
                                      AND r.client_id = w.client_id)")"
if [ "${NORATE:-0}" -eq 0 ]; then
    ok "every pairing has a rate"
else
    bad "$NORATE pairing(s) with no rate — a deposit there opens no trade"
fi

STALE="$(q "SELECT count(*) FROM (
             SELECT DISTINCT ON (supplier_id, client_id) created_at
             FROM rates ORDER BY supplier_id, client_id, created_at DESC) t
           WHERE created_at < now() - interval '24 hours'")"
if [ "${STALE:-0}" -eq 0 ]; then
    ok "no rate is over 24h old"
else
    warn "$STALE rate(s) over 24h old — the bot will flag these on the next deposit"
fi

NOACC="$(q "SELECT count(*) FROM parties p WHERE p.role='supplier' AND p.is_active
            AND NOT EXISTS (SELECT 1 FROM bank_accounts b
                            WHERE b.party_id = p.id AND b.is_active)")"
if [ "${NOACC:-0}" -eq 0 ]; then
    ok "every supplier has a bank account registered"
else
    warn "$NOACC supplier(s) with no bank account — no instruction can be issued for them"
fi

# ---------------------------------------------------------------- monitor
head_ "Monitor"

TOTAL_W="$(q 'SELECT count(*) FROM wallets WHERE is_monitored')"
ADOPTED="$(q 'SELECT count(*) FROM monitor_state WHERE adopted_at_ms IS NOT NULL')"
if [ "${ADOPTED:-0}" -eq "${TOTAL_W:-0}" ] && [ "${TOTAL_W:-0}" -gt 0 ]; then
    ok "all $TOTAL_W monitored wallet(s) adopted"
else
    bad "$ADOPTED of $TOTAL_W wallet(s) adopted — the rest are not being watched"
fi

STALEPOLL="$(q "SELECT count(*) FROM monitor_state
                WHERE last_polled_at < now() - interval '2 minutes'")"
if [ "${STALEPOLL:-0}" -eq 0 ]; then
    ok "every wallet polled within the last 2 minutes"
else
    bad "$STALEPOLL wallet(s) not polled recently — deposits are being missed"
fi

ERRS="$(q 'SELECT coalesce(max(consecutive_errors),0) FROM monitor_state')"
if [ "${ERRS:-0}" -eq 0 ]; then
    ok "no consecutive API errors"
else
    warn "highest consecutive error count is $ERRS"
fi

# ---------------------------------------------------------------- ledger
head_ "Ledger"

echo "  $(q 'SELECT count(*) FROM trades') trade(s), \
$(q 'SELECT count(*) FROM payments') payment(s), \
$(q 'SELECT count(*) FROM deposits') deposit(s)"

ORPHAN="$(q "SELECT count(*) FROM deposits WHERE trade_id IS NULL AND status <> 'unallocated'")"
if [ "${ORPHAN:-0}" -eq 0 ]; then
    ok "no deposit is missing its trade"
else
    warn "$ORPHAN deposit(s) with no trade attached"
fi

MULTI="$(q "SELECT count(*) FROM (SELECT wallet_id FROM trades
            WHERE status IN ('open','awaiting_payment')
            GROUP BY wallet_id HAVING count(*) > 1) t")"
if [ "${MULTI:-0}" -eq 0 ]; then
    ok "at most one open trade per wallet"
else
    bad "$MULTI wallet(s) with more than one open trade"
fi

# ---------------------------------------------------------------- logs
head_ "Recent log"

RECENT_ERR="$(docker compose logs --tail=300 bot 2>/dev/null \
              | grep -cE 'ERROR|Traceback|CRITICAL')"
if [ "${RECENT_ERR:-0}" -eq 0 ]; then
    ok "no errors in the last 300 log lines"
else
    warn "$RECENT_ERR error line(s) in the last 300 — docker compose logs --tail=300 bot | grep -E 'ERROR|Traceback'"
fi

if docker compose logs --tail=300 bot 2>/dev/null | grep -q 'MONITOR_ASSET=TRX'; then
    bad "the bot is running in TRX TEST MODE, not USDT"
else
    ok "not in TRX test mode"
fi

# ---------------------------------------------------------------- backups
head_ "Backups"

BDIR="${BACKUP_DIR:-/var/backups/settlement}"

if crontab -l 2>/dev/null | grep -q 'docker-backup.sh'; then
    ok "nightly backup is scheduled"
else
    bad "NO backup cron installed — nothing is being backed up"
fi

if [ -d "$BDIR" ]; then
    LATEST="$(ls -1t "$BDIR"/settlement-*.sql.gz 2>/dev/null | head -1)"
    if [ -n "$LATEST" ]; then
        AGE_H=$(( ( $(date +%s) - $(stat -c %Y "$LATEST") ) / 3600 ))
        SIZE=$(stat -c %s "$LATEST")
        if [ "$AGE_H" -le 26 ] && [ "$SIZE" -ge 1024 ]; then
            ok "latest backup ${AGE_H}h old, $(du -h "$LATEST" | cut -f1)"
        elif [ "$SIZE" -lt 1024 ]; then
            bad "latest backup is only ${SIZE} bytes — almost certainly empty"
        else
            bad "latest backup is ${AGE_H}h old — nightly backups are not running"
        fi
        if gzip -t "$LATEST" 2>/dev/null; then
            ok "latest backup passes an integrity check"
        else
            bad "latest backup is CORRUPT"
        fi
    else
        bad "no backup files in $BDIR"
    fi
else
    bad "$BDIR does not exist — backups have never run"
fi

# ---------------------------------------------------------------- summary
head_ "Summary"

if [ "$FAILED" -eq 0 ] && [ "$WARNED" -eq 0 ]; then
    printf "  ${G}Everything passed.${N}\n\n"
    exit 0
elif [ "$FAILED" -eq 0 ]; then
    printf "  ${Y}%d warning(s), nothing failed.${N}\n\n" "$WARNED"
    exit 0
else
    printf "  ${R}%d FAILED${N}, %d warning(s).\n\n" "$FAILED" "$WARNED"
    exit 1
fi
