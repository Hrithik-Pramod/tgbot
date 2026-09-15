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
# rebuild leaves old code running and looks entirely normal — which has caught
# us twice, once with a router fix and once with the poll pacing.
#
# Compare the source itself rather than looking for a marker file. Hash the
# contents only, not the paths, since they differ between the repo and /app.
HOST_SRC="$(find core bot monitor db main.py config.py -name '*.py' \
            -exec md5sum {} + 2>/dev/null | awk '{print $1}' | sort | md5sum | cut -d' ' -f1)"
CONT_SRC="$(docker compose exec -T bot sh -c \
            "find /app/core /app/bot /app/monitor /app/db /app/main.py /app/config.py -name '*.py' -exec md5sum {} + 2>/dev/null | awk '{print \$1}' | sort | md5sum" \
            2>/dev/null | cut -d' ' -f1)"

if [ -z "$CONT_SRC" ]; then
    warn "could not read the container's source to compare"
elif [ "$HOST_SRC" = "$CONT_SRC" ]; then
    ok "running image matches the working tree"
else
    bad "the container is running DIFFERENT code from the repo — docker compose build bot && docker compose up -d bot"
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

# DATABASE_URL deliberately does NOT come from .env — docker-compose.yml sets
# it on the container so that inside the network the host is always "db", and
# a stale .env cannot point the bot at the wrong database. So check the
# container's environment, not the file.
DBURL_IN_CONTAINER="$(docker compose exec -T bot printenv DATABASE_URL 2>/dev/null | tr -d '\r')"
case "$DBURL_IN_CONTAINER" in
    postgresql://*@db:*) ok "DATABASE_URL points at the db container" ;;
    "")                  bad "DATABASE_URL is not set inside the container" ;;
    *)                   warn "DATABASE_URL is set but not to the db host: ${DBURL_IN_CONTAINER%%:*}..." ;;
esac

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

# ---------------------------------------------------------------- membership
head_ "Group membership"

# A bot can be alive, its chat registered, its settings perfect — and it can
# still have been removed from the group. Nothing else in this file notices.
#
# On 10 September 2026 the client's side removed the Client Desk bot because it
# was replying to conversation. Every other check here passed while the bot was
# banned and completely unable to reach the people it exists to serve. This is
# the check that would have caught it in seconds.
#
# Removing a member from a supergroup also BANS them, so re-adding needs the
# ban lifting first — which only an admin of that group can do.

bot_id_for() {
    curl -s --max-time 15 "https://api.telegram.org/bot$1/getMe" \
        | sed -n 's/.*"id":\([0-9]*\).*/\1/p' | head -1
}

TMP_PARTIES="/tmp/healthcheck-parties.$$"

member_check() {
    role="$1"; token="$2"
    if [ -z "$token" ]; then bad "$role — no token configured"; return; fi

    botid="$(bot_id_for "$token")"
    if [ -z "$botid" ]; then bad "$role — could not identify the bot"; return; fi

    q "SELECT label || '|' || telegram_chat_id FROM parties
       WHERE role = '$role' AND is_active" > "$TMP_PARTIES"

    # Read from a file, not a pipe: a piped while-loop runs in a subshell and
    # its FAIL counts would be discarded — which would make this check lie in
    # exactly the way it exists to prevent.
    while IFS='|' read -r label chat; do
        [ -z "${chat:-}" ] && continue
        st="$(curl -s --max-time 15 \
              "https://api.telegram.org/bot$token/getChatMember?chat_id=$chat&user_id=$botid" \
              | python3 -c 'import sys,json
r = json.load(sys.stdin)
print(r["result"]["status"] if r.get("ok") else "NOT IN GROUP - " + r.get("description",""))' \
              2>/dev/null)"
        case "$st" in
            member|administrator|creator)
                ok "$label — bot present ($st)" ;;
            "") bad "$label — could not check membership" ;;
            *)  bad "$label — $st" ;;
        esac
    done < "$TMP_PARTIES"

    rm -f "$TMP_PARTIES"
}

member_check bridge   "${BRIDGE_BOT_TOKEN:-}"
member_check supplier "${SUPPLIER_BOT_TOKEN:-}"
member_check client   "${CLIENT_BOT_TOKEN:-}"

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
    q "SELECT '        needs /setrate: ' || s.label || ' -> ' || c.label
       FROM wallets w
       JOIN parties s ON s.id = w.supplier_id
       JOIN parties c ON c.id = w.client_id
       WHERE w.is_internal AND NOT EXISTS (
         SELECT 1 FROM rates r WHERE r.supplier_id = w.supplier_id
                                 AND r.client_id = w.client_id)"
fi

# The same threshold the bot itself uses, read from the same place.
#
# This was hardcoded at 24 hours while the bot read RATE_STALENESS_HOURS, so
# raising the setting would have left the health check still reporting rates
# as stale that the bot had stopped flagging — two answers to one question,
# which is how a check stops being believed.
STALE_H="$(grep -E '^RATE_STALENESS_HOURS=' /opt/settlement-bot/.env 2>/dev/null \
           | tail -1 | cut -d= -f2 | tr -d '[:space:]')"
STALE_H="${STALE_H:-24}"

STALE="$(q "SELECT count(*) FROM (
             SELECT DISTINCT ON (supplier_id, client_id) created_at
             FROM rates ORDER BY supplier_id, client_id, created_at DESC) t
           WHERE created_at < now() - interval '$STALE_H hours'")"
if [ "${STALE:-0}" -eq 0 ]; then
    ok "no rate is older than ${STALE_H}h"
else
    warn "$STALE rate(s) over ${STALE_H}h old — the bot will flag these on the next deposit"
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

# How long the bot has been up. Adoption is staggered across the poll interval
# — roughly a few seconds per wallet — so immediately after a restart most
# wallets legitimately have no cursor yet.
#
# Reporting that as a failure is worse than not checking: a check that cries
# wolf teaches you to skim past it, and this one exists to catch a wallet that
# genuinely is not being watched.
UPTIME_S=0
STARTED="$(docker inspect -f '{{.State.StartedAt}}' \
           "$(docker compose ps -q bot 2>/dev/null)" 2>/dev/null)"
if [ -n "$STARTED" ]; then
    STARTED_EPOCH="$(date -d "$STARTED" +%s 2>/dev/null || echo 0)"
    [ "$STARTED_EPOCH" -gt 0 ] && UPTIME_S=$(( $(date +%s) - STARTED_EPOCH ))
fi

if [ "${ADOPTED:-0}" -eq "${TOTAL_W:-0}" ] && [ "${TOTAL_W:-0}" -gt 0 ]; then
    ok "all $TOTAL_W monitored wallet(s) adopted"
elif [ "$UPTIME_S" -lt 90 ]; then
    warn "$ADOPTED of $TOTAL_W wallet(s) adopted, but the bot started ${UPTIME_S}s ago — still working through them, re-run in a minute"
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

# Only INTERNAL wallets open trades. A deposit on a counterparty wallet is the
# Bridge paying a client onward — it is supposed to have no trade, and flagging
# it trained the eye to ignore this line, which is worse than not having it.
ORPHAN="$(q "SELECT count(*) FROM deposits d
             JOIN wallets w ON w.id = d.wallet_id
             WHERE d.trade_id IS NULL AND d.status <> 'unallocated'
               AND w.is_internal")"
if [ "${ORPHAN:-0}" -eq 0 ]; then
    ok "no deposit is missing its trade"
else
    warn "$ORPHAN deposit(s) with no trade attached"
fi

# A wallet may legitimately have two open trades since 11 Sep 2026: one
# awaiting payment against an issued instruction, and a newer one collecting
# deposits. What must never happen is two trades both open to deposits, because
# then nothing decides which one an arriving deposit belongs to.
# An uninstructed trade left open is the shape of the 15 September incident:
# SUPA5 sat for three days, absorbed a new deposit, and nothing had ever
# mentioned it. A deposit can no longer join a trade that stale — but the
# trade itself still needs someone to deal with it, so say so.
STALE_T="$(q "SELECT count(*) FROM trades
              WHERE status IN ('open','awaiting_payment') AND instructed_at IS NULL
                AND opened_at < now() - interval '6 hours'")"
if [ "${STALE_T:-0}" -eq 0 ]; then
    ok "no trade left open and uninstructed"
else
    warn "$STALE_T trade(s) open and never issued for over 6h — /issue or /cancel"
    q "SELECT '        ' || t.reference || '  ' || t.usdt_received || ' USDT  since '
              || to_char(t.opened_at,'DD Mon HH24:MI')
       FROM trades t
       WHERE t.status IN ('open','awaiting_payment') AND t.instructed_at IS NULL
         AND t.opened_at < now() - interval '6 hours' ORDER BY t.opened_at"
fi

# The migration must actually have been applied. Without the column the bot
# raises on every deposit, and the failure looks like a monitor outage.
HAS_COL="$(q "SELECT count(*) FROM information_schema.columns
              WHERE table_name='trades' AND column_name='instructed_at'")"
if [ "${HAS_COL:-0}" -eq 1 ]; then
    ok "trades.instructed_at present (migration 003 applied)"
else
    bad "trades.instructed_at MISSING — run deploy/migrate-003-instructed.sql"
fi

HAS_ANN="$(q "SELECT count(*) FROM information_schema.columns
              WHERE table_name='trades' AND column_name='announced_at'")"
if [ "${HAS_ANN:-0}" -eq 1 ]; then
    ok "trades.announced_at present (migration 004 applied)"
else
    bad "trades.announced_at MISSING — run deploy/migrate-004-announced.sql"
fi

# A deposit whose notification has been held far past the release window
# means the sweep is not running. The money is recorded either way, but the
# Bridge does not know it is there.
STUCK="$(q "SELECT count(*) FROM trades
            WHERE announced_at IS NULL
              AND status IN ('open','awaiting_payment')
              AND opened_at < now() - interval '30 minutes'")"
if [ "${STUCK:-0}" -eq 0 ]; then
    ok "no deposit is waiting unannounced"
else
    bad "$STUCK trade(s) held unannounced for over 30 minutes — sweep not running"
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
