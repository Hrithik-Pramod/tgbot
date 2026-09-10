-- Clear the trading history and start the references again from 1.
--
-- For live use, when the first day's orders were shakedown runs rather than
-- real business and everyone wants a clean slate.
--
-- STOP THE BOT FIRST. Clearing monitor_state under a running bot is a race and
-- the bot wins — it re-adopts within one poll and writes fresh cursors before
-- you can restart it.
--
--   docker compose stop bot
--   docker compose exec -T db psql -U settlement -d settlement < deploy/clear-orders.sql
--   docker compose start bot
--
-- KEEPS   parties, groups, bank accounts, rates, wallets — the whole setup.
-- CLEARS  trades, payments, payment slots, deposits, deal counters, and the
--         monitor's position on each wallet.
--
-- HOW THIS DIFFERS FROM reset-uat.sql
--
-- That one wipes the audit log. This one does not. On a live system the audit
-- log is the permanent record of who did what — every rate set, every account
-- registered, every correction and the reason for it. Deleting trades is a
-- housekeeping decision; deleting the record of them is not, and it is not
-- reversible.
--
-- The single exception is account nominations. latest_nomination() reads them
-- back and attaches the most recent one to the next trade a supplier opens, so
-- a nomination left over from a cleared trade would silently pre-select an
-- account on a real one. Those rows go; everything else stays.
--
-- WHAT CLEARING monitor_state MEANS
--
-- Each wallet is adopted again on the next poll: whatever is already on chain
-- is recorded, ignored, and only later transfers count. Anything that arrived
-- while the bot was stopped is therefore NOT picked up. If a supplier has sent
-- and it has not been processed yet, deal with that trade before running this.

BEGIN;

DELETE FROM payments;
DELETE FROM payment_slots;
DELETE FROM deposits;
DELETE FROM trades;

-- References start again at SUPA1, SUPB1.
UPDATE supplier_counters SET last_number = 0;

-- Re-adopt every wallet, so existing chain history is ignored rather than
-- read as a fresh pile of deposits.
DELETE FROM monitor_state;

-- The one audit action that feeds back into future behaviour.
DELETE FROM audit_log WHERE action = 'trade.account_nominated';

-- Leave a marker, so the gap in the ledger is explained rather than mysterious.
INSERT INTO audit_log (actor_party_id, action, entity_type, detail)
VALUES (NULL, 'admin.orders_cleared', 'system',
        '{"reason": "start fresh after first-day shakedown"}'::jsonb);

COMMIT;

SELECT 'trades' AS table_name, count(*) FROM trades
UNION ALL SELECT 'payments', count(*) FROM payments
UNION ALL SELECT 'payment_slots', count(*) FROM payment_slots
UNION ALL SELECT 'deposits', count(*) FROM deposits
UNION ALL SELECT 'monitor_state', count(*) FROM monitor_state
UNION ALL SELECT 'parties (kept)', count(*) FROM parties
UNION ALL SELECT 'bank_accounts (kept)', count(*) FROM bank_accounts
UNION ALL SELECT 'rates (kept)', count(*) FROM rates
UNION ALL SELECT 'wallets (kept)', count(*) FROM wallets
UNION ALL SELECT 'audit_log (kept)', count(*) FROM audit_log;
