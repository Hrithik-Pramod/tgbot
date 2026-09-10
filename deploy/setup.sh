#!/usr/bin/env bash
#
# One-shot server setup for the settlement bot.
#
# Target: a fresh Ubuntu 24.04 LTS server. Run as root:
#
#     git clone <repo> /opt/settlement-bot
#     cd /opt/settlement-bot
#     bash deploy/setup.sh
#
# Then fill in /opt/settlement-bot/.env, load the seed data, and start it.
# The script tells you exactly what to do at the end.
#
# Safe to run more than once — every step checks before acting.

set -euo pipefail

APP_DIR="${APP_DIR:-/opt/settlement-bot}"
APP_USER="${APP_USER:-settlement}"
DB_NAME="${DB_NAME:-settlement}"
DB_USER="${DB_USER:-settlement}"

say()  { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run this as root (sudo bash deploy/setup.sh)"
[[ -f "$APP_DIR/main.py" ]] || die "expected the application at $APP_DIR (set APP_DIR to override)"

# ---------------------------------------------------------------- packages
say "Installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
    python3 python3-venv python3-pip \
    postgresql postgresql-contrib \
    ufw curl ca-certificates

# ---------------------------------------------------------------- user
if id -u "$APP_USER" >/dev/null 2>&1; then
    say "User $APP_USER already exists"
else
    say "Creating system user $APP_USER"
    # No login shell and no home directory: this account exists only to own the
    # process. If the bot is ever compromised, it is not a usable foothold.
    adduser --system --group --no-create-home --shell /usr/sbin/nologin "$APP_USER"
fi

# ---------------------------------------------------------------- database
say "Setting up PostgreSQL"
systemctl enable --now postgresql

if sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$DB_USER'" | grep -q 1; then
    say "Database role $DB_USER already exists"
else
    DB_PASS="$(head -c 32 /dev/urandom | base64 | tr -d '/+=' | head -c 32)"
    sudo -u postgres psql -q <<SQL
CREATE ROLE $DB_USER LOGIN PASSWORD '$DB_PASS';
SQL
    echo "$DB_PASS" > /root/.settlement-db-password
    chmod 600 /root/.settlement-db-password
    warn "Generated database password, saved to /root/.settlement-db-password"
fi

if sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'" | grep -q 1; then
    say "Database $DB_NAME already exists — leaving it alone"
else
    say "Creating database $DB_NAME"
    sudo -u postgres createdb -O "$DB_USER" "$DB_NAME"
    say "Applying schema"
    sudo -u postgres psql -q -d "$DB_NAME" -f "$APP_DIR/db/schema.sql"
    sudo -u postgres psql -q -d "$DB_NAME" <<SQL
GRANT ALL ON ALL TABLES IN SCHEMA public TO $DB_USER;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO $DB_USER;
SQL
fi

# ---------------------------------------------------------------- python
say "Creating the virtualenv"
if [[ ! -d "$APP_DIR/.venv" ]]; then
    python3 -m venv "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

# ---------------------------------------------------------------- env file
if [[ -f "$APP_DIR/.env" ]]; then
    say ".env already exists — not touching it"
else
    say "Creating .env from the template"
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    if [[ -f /root/.settlement-db-password ]]; then
        DB_PASS="$(cat /root/.settlement-db-password)"
        sed -i "s|^DATABASE_URL=.*|DATABASE_URL=postgresql://$DB_USER:$DB_PASS@localhost:5432/$DB_NAME|" \
            "$APP_DIR/.env"
    fi
fi

# .env holds three bot tokens and the database password. Nothing but the app
# user needs to read it.
chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
chmod 600 "$APP_DIR/.env"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# ---------------------------------------------------------------- service
say "Installing the systemd service"
cp "$APP_DIR/deploy/settlement-bot.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable settlement-bot >/dev/null 2>&1 || true

# ---------------------------------------------------------------- backups
say "Scheduling nightly backups"
chmod +x "$APP_DIR/deploy/backup.sh"
mkdir -p /var/backups/settlement
chown "$APP_USER:$APP_USER" /var/backups/settlement
cat > /etc/cron.d/settlement-backup <<CRON
# Nightly database backup for the settlement bot
0 2 * * * postgres $APP_DIR/deploy/backup.sh >> /var/log/settlement-backup.log 2>&1
CRON

# ---------------------------------------------------------------- firewall
say "Configuring the firewall"
# The bot makes only outbound connections — Telegram and the TRON APIs. Nothing
# needs to reach it from outside, so only SSH is opened.
ufw allow OpenSSH >/dev/null 2>&1 || ufw allow 22/tcp >/dev/null 2>&1 || true
ufw --force enable >/dev/null 2>&1 || warn "could not enable ufw — check manually"

# ---------------------------------------------------------------- done
cat <<DONE

────────────────────────────────────────────────────────────────────
 Server is ready. Three things left, in this order:

 1. Fill in the bot tokens and channel id
        sudo -u $APP_USER nano $APP_DIR/.env

 2. Load the parties, wallets and rates
        Edit $APP_DIR/db/seed.sql first — replace every :GROUP_ID
        placeholder with the real numeric Telegram group id, then:

        sudo -u postgres psql -d $DB_NAME -f $APP_DIR/db/seed.sql

 3. Start it
        systemctl start settlement-bot
        journalctl -u settlement-bot -f

 Verify before the first real trade:
        sudo -u postgres psql -d $DB_NAME -c \\
          "SELECT label, telegram_chat_id FROM parties;"

 The database password is in /root/.settlement-db-password
────────────────────────────────────────────────────────────────────

DONE
