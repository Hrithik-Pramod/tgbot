-- ============================================================================
-- GO-LIVE SEED — Blockcognitive, 10 September 2026
--
-- Run ONCE, against an empty database, with the bot STOPPED.
--
--   docker compose stop bot
--   docker compose exec -T db psql -U settlement -d settlement < deploy/golive-seed.sql
--   docker compose start bot
--
-- Fill in the three chat ids first. Get them with the bot stopped:
--
--   set -a; source .env; set +a
--   for t in "$BRIDGE_BOT_TOKEN" "$SUPPLIER_BOT_TOKEN" "$CLIENT_BOT_TOKEN"; do
--     curl -s "https://api.telegram.org/bot$t/getUpdates" \
--       | python3 -c 'import sys,json
--   for u in json.load(sys.stdin)["result"]:
--       c=(u.get("message") or u.get("my_chat_member") or {}).get("chat")
--       if c: print(c["id"], "|", c.get("title"))'
--   done
--
-- Each bot only ever sees its own groups, so there is no ambiguity about which
-- id belongs to which. Someone must have sent a message in each group first.
--
-- A group id is negative. A supergroup id starts -100. If a group is later
-- UPGRADED to a supergroup its id changes and the bot goes silent in it — that
-- is the single most common cause of "it worked yesterday".
-- ============================================================================

BEGIN;

-- Refuse to run against a database that already holds anything. Seeding twice
-- would duplicate parties, and a second Bridge row makes authorisation
-- ambiguous.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM parties) THEN
        RAISE EXCEPTION
            'parties is not empty — this seed is for a fresh database only';
    END IF;
END $$;


-- ---------------------------------------------------------------- parties
--
-- label        what counterparties see, and what deal references are built
--              from. "Supplier A" gives SUPA1, SUPA2, ... permanently.
-- display_name internal only. Never appears in a message to a counterparty.

-- Chat ids read from getUpdates on 10 September 2026, each from its own bot,
-- so there is no ambiguity about which id belongs to which role.
INSERT INTO parties (role, label, display_name, telegram_chat_id) VALUES
    ('bridge',   'Bridge',     'UI Control',                  -1004374044457),
    ('supplier', 'Supplier A', 'IndoLondon group NEW',        -1003807764378),
    ('client',   'Client A',   'Haze & ProperPay: Exchange',  -1003946162727);


-- ------------------------------------------------------- deal counter
-- SUPA1, SUPA2, ... Runs continuously and never resets.

INSERT INTO supplier_counters (supplier_id, prefix, last_number)
SELECT id, 'SUPA', 0 FROM parties WHERE label = 'Supplier A';


-- ---------------------------------------------------------------- wallets
--
-- INTERNAL   the wallet Supplier A sends USDT TO. This is the routing key:
--            a deposit here opens a trade for Supplier A → Client A.
-- EXTERNAL   Client A's own wallet, where the Bridge sends their USDT on.
--            A deposit here opens no trade; it notifies the client.
--
-- Both addresses were checksum-verified on 10 September 2026 and both are
-- active on chain.

INSERT INTO wallets (address, is_internal, supplier_id, client_id, label, is_monitored)
SELECT 'TKAcuX3wbb2QexrhPQRbVohVJkhvb9GPeL', TRUE, s.id, c.id,
       'Supplier A → Client A', TRUE
FROM parties s, parties c
WHERE s.label = 'Supplier A' AND c.label = 'Client A';

INSERT INTO wallets (address, is_internal, owner_party_id, label, is_monitored)
SELECT 'TXtfrak7La6RTVDmEFvtR4td7N2tp6tYvG', FALSE, id,
       'Client A payout', TRUE
FROM parties WHERE label = 'Client A';


-- ---------------------------------------------------------------- rates
-- Append-only. /setrate adds a row; nothing is ever overwritten.

INSERT INTO rates (supplier_id, client_id, supply_rate, sell_rate, set_by)
SELECT s.id, c.id, 106, 107, b.id
FROM parties s, parties c, parties b
WHERE s.label = 'Supplier A' AND c.label = 'Client A' AND b.label = 'Bridge';


COMMIT;


-- ---------------------------------------------------------------- verify
SELECT role, label, display_name, telegram_chat_id FROM parties ORDER BY role;

SELECT CASE WHEN w.is_internal THEN 'internal' ELSE 'external' END AS kind,
       w.address, w.label, w.is_monitored
FROM wallets w ORDER BY w.is_internal DESC;

SELECT s.label AS supplier, c.label AS client, r.supply_rate, r.sell_rate
FROM rates r
JOIN parties s ON s.id = r.supplier_id
JOIN parties c ON c.id = r.client_id;

SELECT 'monitor_state (must be empty — wallets adopt on first poll)' AS note,
       count(*) FROM monitor_state;


-- ============================================================================
-- ADDING SUPPLIER B LATER
--
-- A second supplier does NOT need a wipe or a restart. Three inserts, and the
-- monitor picks the new wallet up on its next pass because it re-reads the
-- wallet list every cycle.
--
-- Needed first: the group's chat id, the internal wallet address for
-- Supplier B → Client A, and the two rates.
--
--   BEGIN;
--
--   INSERT INTO parties (role, label, display_name, telegram_chat_id)
--   VALUES ('supplier', 'Supplier B', '<group name>', <chat id>);
--
--   INSERT INTO supplier_counters (supplier_id, prefix, last_number)
--   SELECT id, 'SUPB', 0 FROM parties WHERE label = 'Supplier B';
--
--   INSERT INTO wallets (address, is_internal, supplier_id, client_id, label,
--                        is_monitored)
--   SELECT '<internal wallet>', TRUE, s.id, c.id, 'Supplier B → Client A', TRUE
--   FROM parties s, parties c
--   WHERE s.label = 'Supplier B' AND c.label = 'Client A';
--
--   COMMIT;
--
-- Then set the rate with /setrate rather than SQL — it is append-only either
-- way, and doing it through the command puts it in the audit log with who set
-- it. The wallet adopts itself on the next poll and announces in the Bridge
-- channel how many existing transactions it ignored.
-- ============================================================================
