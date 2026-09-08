# USDT / INR Settlement Bot

Telegram-based settlement and reconciliation for USDT/INR trades between
multiple suppliers and multiple clients, with a Bridge operator in the middle.

**The bot never holds private keys, never initiates transfers, and never takes
custody of funds** (confirmed, H3). It monitors wallets, performs calculations,
and sends notifications. Every transfer is actioned manually by the Bridge.

---

## How the money moves

Two sides move independently:

| Leg | Direction |
|---|---|
| **USDT** | Supplier → Bridge internal wallet → Client (minus commission) |
| **INR** | Client → **directly into the supplier's bank account** |

The Bridge never touches INR. The margin stays as USDT in the internal wallet.

**Worked example** (supply rate 105.50, sell rate 106.50):

```
Supplier deposits          1,000.00 USDT
INR obligation             1,000 x 105.50     = Rs 105,500
USDT owed to client        105,500 / 106.50   =    990.61 USDT
Bridge margin                                 =      9.39 USDT   (0.94%)
```

---

## Roles

Three bots, one codebase, one database. Separate tokens so Telegram shows each
party only its own command menu, and so a leaked supplier token cannot reach the
Bridge.

| Bot | Commands |
|---|---|
| **Bridge** | `/setrate` `/wallet` `/walletchange` `/send` `/summary` `/cancel` `/correct` `/export` |
| **Supplier** | `/account` `/account_remove` `/send` |
| **Client** | `/accounts` `/add` `/done` |

### The trade cycle

1. Supplier deposits USDT into the internal wallet for that supplier→client pairing.
2. Monitor detects it, opens or extends the trade, and posts to your channel with
   a **Confirm and issue to client** button.
3. You tap it. The account the supplier nominated at `/send` is shown already
   ticked — one tap — but you can override it or split across several accounts.
   Amounts are checked so the total can never exceed the deposit.
4. You approve the preview; the instruction goes to the client.
5. Client logs each tranche with `/add` (amount, UTR, which account).
6. Client sends `/done`. The summary goes to all three of you.

---

## Setup

Requires Python 3.11+ and PostgreSQL 14+.

**On a server** — one script does everything:

```bash
git clone <repo> /opt/settlement-bot
cd /opt/settlement-bot
sudo bash deploy/setup.sh
```

It installs packages, creates the database and system user, applies the schema,
builds the virtualenv, installs the systemd service, schedules nightly backups,
and configures the firewall. Then it tells you the three things left to do.

**Locally**:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

createdb settlement
psql settlement < db/schema.sql
psql settlement < db/seed.sql     # edit the :GROUP_ID placeholders first

cp .env.example .env              # then fill it in
python main.py
```

Day-to-day operations, troubleshooting and recovery are in
[`deploy/RUNBOOK.md`](deploy/RUNBOOK.md).

### Getting the values for `.env`

- **Bot tokens** — create three bots with [@BotFather](https://t.me/BotFather).
- **`BRIDGE_CHANNEL_ID`** — add the Bridge bot to your group, post a message,
  then read the chat id from the bot's `getUpdates`. Group ids are negative.
- **`BRIDGE_USER_ID`** — optional; only used when `STRICT_BRIDGE_USER=true`.

### Registering parties

Suppliers and clients are added manually by the Bridge (decision D1). There is
no self-service onboarding by design. A party is keyed on its **group chat id** —
that is what grants access.

```sql
-- telegram_chat_id is the security boundary: whoever is in this group
-- can act as Supplier A.
INSERT INTO parties (role, label, display_name, telegram_chat_id)
VALUES ('supplier', 'Supplier A', 'Internal note', -1001234567890);

INSERT INTO supplier_counters (supplier_id, prefix)
VALUES (currval('parties_id_seq'), 'SUPA');

-- One internal wallet per supplier→client pairing. The wallet IS the routing key.
INSERT INTO wallets (address, is_internal, supplier_id, client_id, label)
VALUES ('T...', TRUE, 1, 2, 'Supplier A / Client A');
```

---

## Running the tests

```bash
pytest -q
```

105 tests, of which 17 run against a real PostgreSQL instance.

The pure-logic tests reproduce a representative settled trade — the six tranches
totalling ₹1,499,297 — and assert the summary renders character for character in
their format. The integration tests apply the real schema and walk a complete
trade through `db/repo.py`: deposit, rate lookup, slot issuance, six payments,
duplicate rejection, completion.

The integration tests need a database. They skip themselves if there isn't one,
so `pytest -q` works anywhere. To run them:

```bash
pip install pgserver     # bundles its own PostgreSQL, no root needed
pytest tests/test_integration.py -v
```

Or set `DATABASE_URL` to point at any PostgreSQL instance.

---

## Design decisions worth knowing

**Every monetary value is a `Decimal`.** `core.money.to_decimal` rejects
`float` outright rather than converting it. A float anywhere in this path would
silently corrupt the ledger, and the schema uses `NUMERIC` throughout for the
same reason.

**The monitor is at-least-once, never at-most-once.** Restarts and overlapping
poll windows re-deliver transactions. Correctness comes from the
`UNIQUE (tx_hash, wallet_id)` constraint, not from the poller being careful.
Missing a deposit is unrecoverable; seeing one twice is free.

**The cursor rewinds two minutes on every poll.** Block timestamps are not
perfectly ordered, and a strict "newer than last seen" filter drops boundary
transactions.

**Authorisation is by chat, not by individual user** (client decision,
7 Sep 2026). A party is identified by the group a command arrives in, so
whoever the Bridge adds to a registered group can act as that party. Access is
controlled by controlling group membership.

The compensating controls are alerts, not gates: every bank account added or
removed, and every person who joins or leaves a registered group, is reported
to the Bridge channel. `STRICT_BRIDGE_USER=true` re-locks the Bridge bot to a
single user id without a code change.

Commands from unregistered chats are ignored silently — an error reply would
confirm the bot exists and reveal what it does.

**Rates are append-only.** `/setrate` inserts rather than updates, and each
trade snapshots the rate it opened under, so a later rate change cannot rewrite
history.

**Bank accounts are soft-deleted.** `/account_remove` deactivates rather than
deletes, because historic payments reference those rows and old summaries must
stay readable.

**Totals are recomputed from the running sum, not accumulated.** A sum of
rounded parts is not the rounding of a sum.

---

## Confirmed requirements

| | Decision |
|---|---|
| Network | TRON (TRC20) only, USDT only |
| Wallets | Under 10 at launch; one internal wallet per pairing |
| Rate format | INR per 1 USDT |
| Rounding | USDT 2 dp, INR whole rupees, half up |
| Pairing | Inferred from the receiving internal wallet |
| Partial deposits | Accumulate against one open trade |
| Deal reference | `SUPA1` — continuous per supplier, never resets |
| Duplicate UTR | Rejected, client told |
| Tolerance | 1 USDT, converted at the trade's sell rate |
| Number format | Western grouping |
| Identities | Labels only between counterparties |
| KYC | Handled outside the bot |
| Authorisation | By group membership — whoever the Bridge adds to a group can use that bot |
| Receiving account | One per trade, nominated by the supplier at `/send` |
| Retention | Strict monthly rolling — trades older than a month cannot be corrected |

---

## Still open

Nothing blocking. All requirements are answered and built.

- **Notification wording** — sent for review as document 02, not yet returned.
- **Retention** — the client chose strict monthly rolling, accepting that trades
  older than a month cannot be corrected. Worth revisiting if it ever bites; it
  is a one-line change.

### A note on the authorisation model

The client asked on 7 Sep 2026 for open access by group membership, replacing
per-user checks. This was implemented as instructed. The consequence is that
**anyone in a supplier group can register a bank account**, and that account can
reach a client's payment instruction. The client was told this in writing and
accepted it; the compensating controls are the account-change and group-join
alerts to the Bridge channel.

---

## Not yet built

- Live testing against a real TRON wallet (Stage 2 of the go-live plan). This is
  the only untested path: the monitor has been exercised against fixtures and a
  real database, but never against a live TRON address.

Waiting on the client: three bot tokens and the numeric group ids. Wallet
addresses and opening rates are supplied and loaded into `db/seed.sql`.
