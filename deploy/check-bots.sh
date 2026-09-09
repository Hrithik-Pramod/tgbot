#!/usr/bin/env bash
#
# Check whether the three bot tokens are alive.
#
#   ./deploy/check-bots.sh                 # reads tokens from .env
#   ./deploy/check-bots.sh <t1> <t2> <t3>  # or pass them directly
#
# Reports exactly what Telegram says rather than interpreting it, so it stays
# correct even if the API's wording changes.
#
# Reading the result:
#
#   ok: true                 the bot is alive and the token is current
#   401 Unauthorized         the token is wrong or has been revoked/reset
#   403 / "frozen"           the bot exists but is restricted — appeal pending
#
# Note the difference between the middle two. After Telegram resets a token,
# the OLD one returns 401 even once the bot is unfrozen — that is a stale
# token, not a freeze. Always test the token currently shown in @BotFather.

set -uo pipefail
cd "$(dirname "$0")/.."

if [[ $# -eq 3 ]]; then
    TOKENS=("$1" "$2" "$3")
    NAMES=("bot 1" "bot 2" "bot 3")
elif [[ -f .env ]]; then
    # shellcheck disable=SC1091
    set -a; source .env; set +a
    TOKENS=("${BRIDGE_BOT_TOKEN:-}" "${SUPPLIER_BOT_TOKEN:-}" "${CLIENT_BOT_TOKEN:-}")
    NAMES=("bridge" "supplier" "client")
else
    echo "No .env found. Pass the three tokens as arguments instead." >&2
    exit 1
fi

alive=0
for i in 0 1 2; do
    name="${NAMES[$i]}"
    token="${TOKENS[$i]}"

    if [[ -z "$token" ]]; then
        printf '%-10s  NOT SET\n' "$name"
        continue
    fi

    body=$(curl -s --max-time 15 "https://api.telegram.org/bot${token}/getMe")

    if [[ -z "$body" ]]; then
        printf '%-10s  NO RESPONSE — network problem, not a bot problem\n' "$name"
        continue
    fi

    if grep -q '"ok":true' <<<"$body"; then
        username=$(sed -n 's/.*"username":"\([^"]*\)".*/\1/p' <<<"$body")
        printf '%-10s  ALIVE      @%s\n' "$name" "$username"
        alive=$((alive + 1))
    else
        # Print Telegram's own words. Do not paraphrase — the distinction
        # between a revoked token and a frozen bot is in this string.
        desc=$(sed -n 's/.*"description":"\([^"]*\)".*/\1/p' <<<"$body")
        code=$(sed -n 's/.*"error_code":\([0-9]*\).*/\1/p' <<<"$body")
        printf '%-10s  BLOCKED    %s %s\n' "$name" "${code:-?}" "${desc:-$body}"
    fi
done

echo
if [[ $alive -eq 3 ]]; then
    echo "All three bots are alive. Safe to proceed."
else
    echo "$alive of 3 alive. Do not deploy until all three answer."
fi
