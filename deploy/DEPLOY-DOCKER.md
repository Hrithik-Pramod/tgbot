# Deploying with Docker

Start to finish on a fresh Ubuntu server. Roughly 20 minutes, most of it
waiting for the image to build.

---

## Before you start

You need three things. Without them the stack will come up and then sit there
ignoring every message, which looks like a fault but isn't.

1. **Three bot tokens** from [@BotFather](https://t.me/BotFather)
2. **The numeric group ids** — negative numbers, typically `-100…`
3. **The real `db/seed.sql`** — it is gitignored, so `git clone` will *not*
   bring it. Copy it up separately (see step 4).

---

## 0. Turn OFF privacy mode on the supplier and client bots

**Do this before adding the bots to any group.** By default a Telegram bot in a
group only sees messages that start with `/` or that mention it by name. Under
that default the paste parser never sees anything: the client sends a payment,
the bot receives nothing, and there is no error anywhere to explain it.

In [@BotFather](https://t.me/BotFather):

```
/mybots  →  choose the bot  →  Bot Settings  →  Group Privacy  →  Turn off
```

Do it for the **supplier** and **client** bots. The Bridge bot only takes
commands, so it can keep privacy mode on.

**If a bot is already in a group, remove it and add it again** — the change does
not apply to existing memberships.

Verify: send a plain message (no slash) in a group and check
`getUpdates` shows it.

```bash
curl -s "https://api.telegram.org/bot<CLIENT_TOKEN>/getUpdates" | python3 -m json.tool
```

If plain messages do not appear, privacy mode is still on.

---

## 1. Install Docker

```bash
curl -fsSL https://get.docker.com | sh
docker --version
docker compose version
```

## 2. Clone

```bash
sudo mkdir -p /opt/settlement-bot
sudo chown "$USER" /opt/settlement-bot
git clone https://github.com/Hrithik-Pramod/tgbot.git /opt/settlement-bot
cd /opt/settlement-bot
```

## 3. Configure

```bash
cp .env.example .env

# Generate a database password
echo "POSTGRES_PASSWORD=$(openssl rand -base64 24 | tr -d '/+=')" >> .env

nano .env      # fill in the three bot tokens and BRIDGE_CHANNEL_ID
```

Set `TZ` while you are in there — `Europe/London` or `Asia/Kolkata` — so log
timestamps read correctly.

```bash
chmod 600 .env     # it holds three tokens and the database password
```

## 4. Get the real seed file onto the server

`db/seed.sql` is deliberately not in the repository: it contains the real wallet
addresses and rates. From your own machine:

```bash
scp "db/seed.sql" user@server:/opt/settlement-bot/db/seed.sql
```

Then on the server, replace every `:GROUP_ID` placeholder with the real numeric
group id:

```bash
nano db/seed.sql
```

## 5. Bring it up

```bash
docker compose up -d --build
docker compose ps
```

The database applies `db/schema.sql` automatically on first start. The bot waits
for the database to pass its healthcheck before starting, so a slow first boot
is normal rather than a failure.

## 6. Seed

Deliberately manual — seeding with placeholder group ids gives you a system that
starts cleanly and then silently ignores everything.

```bash
docker compose exec -T db psql -U settlement -d settlement < db/seed.sql
```

## 7. Verify

```bash
docker compose exec db psql -U settlement -d settlement -c \
  "SELECT label, role, telegram_chat_id FROM parties;"

docker compose exec db psql -U settlement -d settlement -c \
  "SELECT w.label, w.address, w.is_internal FROM wallets w;"

docker compose logs -f bot
```

You are looking for:

```
starting three bots and the deposit monitor
deposit monitor starting
```

Then send `/wallet` in the Bridge group. If it replies, the chat id is right.
**Silence means the group id in the database does not match the real one** — the
bot ignores unregistered chats deliberately.

## 8. Schedule backups

```bash
chmod +x deploy/docker-backup.sh
sudo mkdir -p /var/backups/settlement

echo "0 2 * * * cd /opt/settlement-bot && ./deploy/docker-backup.sh >> /var/log/settlement-backup.log 2>&1" \
  | sudo tee /etc/cron.d/settlement-backup

sudo ./deploy/docker-backup.sh    # prove it works now, not at 2am
```

## 9. Firewall

The bot publishes no ports and makes outbound connections only, so nothing needs
to reach it.

```bash
sudo ufw allow OpenSSH
sudo ufw --force enable
```

---

## Day to day

```bash
docker compose logs -f bot          # watch
docker compose restart bot          # safe at any time
docker compose ps                   # status
docker compose down                 # stop (the ledger survives)
```

**`docker compose down` is safe.** The database lives in a named volume, so
stopping and starting loses nothing.

**Never run `docker compose down -v` on this server.** The `-v` destroys the
volume and the entire ledger with it.

Restarting is always safe: the monitor keeps a per-wallet cursor and rewinds two
minutes on each poll, and duplicate transactions are absorbed by a unique
constraint. Nothing is double-counted and nothing in the gap is missed.

---

## Updating

```bash
cd /opt/settlement-bot
./deploy/docker-backup.sh          # take a dump first, always
git pull
docker compose up -d --build
docker compose logs -f bot
```

The pre-update dump is the only way back.

---

## Schema changes

`db/schema.sql` runs **only** when the volume is first created. A later change
to that file does nothing to a database that already exists — apply migrations
by hand:

```bash
docker compose exec -T db psql -U settlement -d settlement < path/to/migration.sql
```

---

## Troubleshooting

**Bot restarting in a loop**

```bash
docker compose logs --tail=50 bot
```

Usually a missing variable in `.env` — the config raises on start rather than
running half-configured.

**Bot silent in a group.** The chat id is not registered. Compare:

```bash
docker compose exec db psql -U settlement -d settlement -c \
  "SELECT label, telegram_chat_id FROM parties;"

curl -s "https://api.telegram.org/bot<BRIDGE_TOKEN>/getUpdates" | python3 -m json.tool | grep -A2 '"chat"'
```

A group **upgraded to a supergroup gets a new id**, which is the usual cause of
a bot that worked yesterday and does not today. Fix with an UPDATE:

```bash
docker compose exec db psql -U settlement -d settlement -c \
  "UPDATE parties SET telegram_chat_id = -100NEWID WHERE label = 'Supplier A';"
docker compose restart bot
```

**Database will not start**

```bash
docker compose logs db
docker volume ls | grep pgdata
```

**Everything else** — see [`RUNBOOK.md`](RUNBOOK.md). The diagnosis is the same;
prefix the psql commands with `docker compose exec db`.

---

## Restoring

```bash
docker compose stop bot

gunzip -c /var/backups/settlement/settlement-YYYY-MM-DD.sql.gz \
  | docker compose exec -T db psql -U settlement -d settlement

docker compose start bot
```

Restoring loses anything since that night's dump. The monitor re-detects recent
on-chain deposits on its own, but INR payments logged by clients in that window
are gone and must be re-entered.
