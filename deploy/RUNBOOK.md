# Operations Runbook

Everything needed to run, diagnose and recover this system. Written so that
someone who has never seen the code can keep it alive.

---

## Daily reality

The bot needs no daily attention. It restarts itself, backs itself up, and
switches blockchain providers on its own. What follows is for when something
looks wrong.

---

## Health check

```bash
systemctl status settlement-bot          # is it running?
journalctl -u settlement-bot -n 50       # what did it last say?
journalctl -u settlement-bot -f          # watch it live
```

A healthy log looks like this, roughly every 20 seconds of quiet:

```
starting three bots and the deposit monitor
deposit monitor starting
```

Deposits appear as:

```
new deposit 14211.35 USDT to wallet 1 (tx 3f2a91c4...)
trade SUPA1 slots issued and sent to client
```

---

## Start, stop, restart

```bash
systemctl start settlement-bot
systemctl stop settlement-bot
systemctl restart settlement-bot     # safe at any time — see below
```

**Restarting is always safe.** The monitor stores a cursor per wallet and
rewinds two minutes on every poll, so it re-reads recent transactions after a
restart. Duplicates are absorbed by the `UNIQUE (tx_hash, wallet_id)`
constraint. Nothing is double-counted and nothing in the gap is missed.

---

## Common problems

### The bot sees commands but ignores pasted payments

Privacy mode. By default a bot in a group only receives messages beginning with
`/` or mentioning it, so `/add` works and a pasted payment is never delivered —
with no error to explain it.

Fix in @BotFather: `/mybots` → the bot → Bot Settings → Group Privacy → Turn
off. Then **remove the bot from the group and add it again**; the change does
not apply to existing memberships.

Confirm with `getUpdates` that plain messages now appear.

### The bot is not responding to commands

Almost always the chat is not registered. Authorisation is by chat: if the group
is not in the `parties` table, the bot ignores it completely and silently.

```bash
sudo -u postgres psql -d settlement -c \
  "SELECT label, role, telegram_chat_id FROM parties WHERE is_active;"
```

Compare against the group's real id. Get the real id from:

```bash
curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | python3 -m json.tool | grep -A3 '"chat"'
```

Group ids are negative. A supergroup id starts `-100`. **A group that is
upgraded to a supergroup gets a new id**, which is a common cause of a bot that
worked yesterday and does not today.

### A deposit was not detected

Work down this list in order.

```bash
# 1. Is the wallet being monitored?
sudo -u postgres psql -d settlement -c \
  "SELECT id, address, is_internal, is_monitored FROM wallets;"

# 2. Is the address exactly right? Compare character by character.
#    A wrong address produces no error — deposits simply never arrive.

# 3. Has the monitor polled recently?
sudo -u postgres psql -d settlement -c \
  "SELECT wallet_id, last_polled_at, consecutive_errors FROM monitor_state;"

# 4. Did it see the transaction but fail to allocate it?
sudo -u postgres psql -d settlement -c \
  "SELECT tx_hash, amount_usdt, status, trade_id FROM deposits
   ORDER BY detected_at DESC LIMIT 10;"
```

A deposit with `status = 'unallocated'` or a null `trade_id` usually means **no
rate is set** for that supplier/client pairing. The Bridge channel will have
been told. Fix with `/setrate`.

### Both blockchain APIs are failing

The Bridge channel gets an alert after five consecutive failures. Check by hand:

```bash
curl -s "https://apilist.tronscanapi.com/api/token_trc20/transfers?limit=1" | head -c 200
curl -s "https://api.trongrid.io/v1/accounts/TFLEpkCtXFSCYCvzqgtUENDaSUKcFUX2zb/transactions/trc20?limit=1" | head -c 200
```

If TronScan is rate-limiting, adding a free TronGrid API key to `.env` as
`TRONGRID_API_KEY` raises the fallback's ceiling. Restart afterwards.

### The figures look wrong

They are reproducible. Every trade snapshots the rate it opened under, so the
arithmetic can always be re-derived:

```bash
sudo -u postgres psql -d settlement -c \
  "SELECT reference, supply_rate, sell_rate, usdt_received, inr_expected,
          usdt_owed_client, margin_usdt FROM trades ORDER BY opened_at DESC LIMIT 5;"
```

Check by hand: `inr_expected = usdt_received × supply_rate`, and
`usdt_owed_client = inr_expected ÷ sell_rate` rounded to 2 decimal places.

If a rate was changed mid-trade, the trade keeps the rate it started with. That
is deliberate.

### Someone was added to a group who should not have been

Access is by group membership, so removing them from the group removes their
access immediately — no command needed. Then check what they did:

```bash
sudo -u postgres psql -d settlement -c \
  "SELECT created_at, action, detail FROM audit_log
   ORDER BY created_at DESC LIMIT 30;"
```

Pay particular attention to `account.add`. A bank account registered by someone
who should not have had access is the one change that can move money.

```bash
sudo -u postgres psql -d settlement -c \
  "SELECT b.id, p.label, b.account_name, b.account_number, b.ifsc, b.created_at
   FROM bank_accounts b JOIN parties p ON p.id = b.party_id
   WHERE b.is_active ORDER BY b.created_at DESC;"
```

To lock the Bridge bot down to a single user without a code change, set
`STRICT_BRIDGE_USER=true` and `BRIDGE_USER_ID=<your id>` in `.env`, then restart.

---

## Backups

Nightly at 02:00, kept 30 days, in `/var/backups/settlement`.

```bash
ls -lh /var/backups/settlement          # what exists
tail -20 /var/log/settlement-backup.log # did last night work?
bash /opt/settlement-bot/deploy/backup.sh   # run one now
```

### Restoring

```bash
systemctl stop settlement-bot

sudo -u postgres dropdb settlement
sudo -u postgres createdb -O settlement settlement
gunzip -c /var/backups/settlement/settlement-YYYY-MM-DD.sql.gz \
  | sudo -u postgres psql -d settlement

systemctl start settlement-bot
```

Restoring loses anything since that night's dump. Check for deposits in the gap
before assuming the system is back to normal — the monitor will re-detect
recent on-chain deposits on its own, but INR payments logged by clients in that
window are gone and must be re-entered.

### Offsite copies

Set `S3_TARGET` in the backup script's environment. A backup that lives only on
the same server as the database is not a backup.

---

## Rotating bot tokens

Worth doing after handover, and after anyone leaves who had access.

1. Message `@BotFather`, choose the bot, **API Token → Revoke current token**.
2. Put the new token in `/opt/settlement-bot/.env`.
3. `systemctl restart settlement-bot`.

The old token stops working immediately. Group membership and all data are
unaffected.

---

## Changing a wallet address

Use `/walletchange` on the Bridge bot rather than editing the database. It
clears the monitoring cursor for that wallet, so the new address is not scanned
from an offset that belonged to the old one.

---

## Upgrading

```bash
systemctl stop settlement-bot
cd /opt/settlement-bot
sudo -u postgres pg_dump settlement | gzip > /var/backups/settlement/pre-upgrade-$(date +%F).sql.gz
git pull
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q        # must pass before starting
systemctl start settlement-bot
```

Always take the pre-upgrade dump. It is the only way back.

---

## Escalation

If the system is down and the cause is not in this document:

1. `systemctl stop settlement-bot` — stopping is always safe. Nothing is lost;
   deposits are re-detected on the next start.
2. Fall back to the manual process. The bot never moves money, so nothing is
   stuck — you have simply lost the calculations and notifications.
3. Capture the evidence before restarting:

```bash
journalctl -u settlement-bot -n 500 > /tmp/settlement-crash.log
```

There is no state in the bot that only exists in memory. Everything that
matters is in PostgreSQL.
