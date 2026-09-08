-- ============================================================================
-- Seed data TEMPLATE.
--
-- Every value below is synthetic. Copy to db/seed.sql (gitignored) and replace
-- with the real addresses, rates and group ids before running.
--
-- Run AFTER schema.sql:
--     psql settlement < db/schema.sql
--     psql settlement < db/seed.sql
--
-- BEFORE RUNNING: replace every :GROUP_ID placeholder below with the real
-- numeric Telegram group id. The client supplied group NAMES but not IDs.
--
-- To get a group id: add the relevant bot to the group, post any message, then
--     curl "https://api.telegram.org/bot<TOKEN>/getUpdates"
-- and read chat.id. Group ids are negative, typically -100xxxxxxxxxx.
--
-- The example addresses are valid base58check but are NOT real wallets.
-- When you substitute real ones, verify each is well-formed AND is the wallet
-- you intend — a wrong address produces no error, deposits simply never arrive.
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------- parties
-- telegram_chat_id is the security boundary: whoever is in the group can act
-- as that party (client decision, 7 Sep 2026).

INSERT INTO parties (role, label, display_name, telegram_chat_id) VALUES
    ('bridge',   'Bridge',     'Bridge Control',                  -1000000000001),  -- :GROUP_ID  Bridge Control
    ('supplier', 'Supplier A', 'Northgate',                  -1000000000002),  -- :GROUP_ID  Northgate
    ('supplier', 'Supplier B', 'AB group',                    -1000000000003),  -- :GROUP_ID  AB group
    ('client',   'Client A',   'Client A Exchange',  -1000000000004);  -- :GROUP_ID  Client A Exchange

-- ------------------------------------------------------- deal counters
-- E2: SUPA1, SUPA2, ... continuous per supplier, never resets.

INSERT INTO supplier_counters (supplier_id, prefix)
SELECT id, CASE label WHEN 'Supplier A' THEN 'SUPA' ELSE 'SUPB' END
FROM parties WHERE role = 'supplier';

-- ---------------------------------------------------------------- wallets
-- One internal wallet per supplier→client pairing. Both suppliers currently
-- pair only with Client A, so one wallet each.
--
-- ASSUMPTION TO CONFIRM: the client's table gave a wallet per supplier and a
-- separate wallet for Client A, without spelling out the pairings. Read as:
--   TFLEpk… = internal, Supplier A → Client A
--   TJJb8j… = internal, Supplier B → Client A
--   TAYdLT… = Client A's own wallet (external, watched for the outbound leg)

INSERT INTO wallets (address, is_internal, supplier_id, client_id, label)
SELECT 'TFLEpkCtXFSCYCvzqgtUENDaSUKcFUX2zb', TRUE, s.id, c.id, 'Supplier A / Client A'
FROM parties s, parties c WHERE s.label = 'Supplier A' AND c.label = 'Client A';

INSERT INTO wallets (address, is_internal, supplier_id, client_id, label)
SELECT 'TJJb8jUTcrdtq57YrAcyEWhTkECd6dbRbo', TRUE, s.id, c.id, 'Supplier B / Client A'
FROM parties s, parties c WHERE s.label = 'Supplier B' AND c.label = 'Client A';

INSERT INTO wallets (address, is_internal, owner_party_id, label)
SELECT 'TAYdLT7dqiwj1fLhg1pJ7RLVkseW4Ct7DW', FALSE, id, 'Client A wallet'
FROM parties WHERE label = 'Client A';

-- ---------------------------------------------------------------- rates
-- INR per 1 USDT (C1). Append-only — /setrate inserts a new row rather than
-- updating, so historic trades stay reproducible.
--
--   Supplier A: 105.50 / 106.50  ->  0.93% spread
--   Supplier B: 106.50 / 106.50  ->  0.98% spread

INSERT INTO rates (supplier_id, client_id, supply_rate, sell_rate, set_by)
SELECT s.id, c.id, 105.50, 105.00, b.id
FROM parties s, parties c, parties b
WHERE s.label = 'Supplier A' AND c.label = 'Client A' AND b.role = 'bridge';

INSERT INTO rates (supplier_id, client_id, supply_rate, sell_rate, set_by)
SELECT s.id, c.id, 105.00, 105.00, b.id
FROM parties s, parties c, parties b
WHERE s.label = 'Supplier B' AND c.label = 'Client A' AND b.role = 'bridge';

COMMIT;

-- ---------------------------------------------------------------- check
-- Run this after seeding to confirm everything wired up as intended.
--
-- SELECT w.label, w.address, w.is_internal,
--        s.label AS supplier, c.label AS client,
--        r.supply_rate, r.sell_rate
-- FROM wallets w
-- LEFT JOIN parties s ON s.id = w.supplier_id
-- LEFT JOIN parties c ON c.id = w.client_id
-- LEFT JOIN rates r ON r.supplier_id = w.supplier_id AND r.client_id = w.client_id
-- ORDER BY w.is_internal DESC, w.id;
